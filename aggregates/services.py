# aggregates/services.py
"""Формула агрегатов (игроки, тренеры, команды, судьи) и защита оценок.

Вес голоса (build_user_weight_map) считается один раз на матч и зависит от
просмотра, trust_score и истории предвзятости пользователя.
Дополнительные слои защиты:
- в расчёт идут только голоса завершённых сессий без исключённых аккаунтов (countable_evaluations);
- винзоризация хвостов (а для 3-9 голосов — клиппинг по медиане/MAD);
- градуированный штраф веса за систематическую предвзятость к своей команде;
- нейтральный якорь: при большой доле фанатов обеих сторон итог
  подтягивается к мнению нейтральных зрителей с историей оценок.
"""
from __future__ import annotations

import logging
import math
import statistics
import uuid
from collections import Counter, defaultdict
from typing import Iterable

from django.conf import settings
from django.core.cache import cache
from django.db.models import Avg, Count, ExpressionWrapper, F, FloatField, OuterRef, Q, Subquery, Sum, UUIDField
from django.db.models.functions import Coalesce, NullIf

from evaluations.models import ContextEvaluation, EvaluationSession, PlayerEvaluation
from users.models import User

logger = logging.getLogger(__name__)


def vote_weighted_avg(field: str, votes_field: str = "total_votes", filter: Q | None = None) -> ExpressionWrapper:
    """Среднее по матчам с учётом числа голосов: Σ(значение × голосов) / Σ(голосов).

    Для полей через связь передавайте votes_field с тем же префиксом.
    filter — какие строки агрегатов учитывать. Нет голосов — None. Не объявляйте в том же
    запросе алиас total_votes раньше этого выражения — Django примет его за агрегат и упадёт.
    """
    return ExpressionWrapper(
        Sum(F(field) * F(votes_field), output_field=FloatField(), filter=filter)
        / NullIf(Sum(votes_field, filter=filter), 0),
        output_field=FloatField(),
    )

FAN_BIAS_CACHE_TTL = 600  # секунд
FAN_BIAS_MIN_HISTORY_MATCHES = 3
FAN_BIAS_LOOKBACK_MATCHES = 10
FAN_BIAS_EXTREME_TEAM_SCORE = 9
FAN_BIAS_EXTREME_OPPONENT_SCORE = 3
FAN_BIAS_THRESHOLD_RATIO = 0.7

# --- Градуированный штраф за систематическую предвзятость ---
# BIAS_FREE_DIFF — допустимая «тёплая» разница своим/чужим, не штрафуется.
BIAS_FREE_DIFF = 1.0
# После этой разницы штраф больше не растёт.
BIAS_MAX_DIFF = 7.0
# Потолок штрафа.
BIAS_CONTINUOUS_MAX_PENALTY = 0.5
# Ниже этого разброса разница «свои − чужие» подозрительно стабильна (похоже на накрутку).
BIAS_LOW_VARIANCE_STDEV = 1.0
BIAS_LOW_VARIANCE_MULTIPLIER = 1.25

# Штраф от этого порога создаёт флаг extreme_bias для модератора.
# Фактический порог калибруется (users.tasks.ANTIFRAUD_CALIBRATED_THRESHOLDS).
EXTREME_BIAS_FLAG_THRESHOLD = 0.2

# --- Нейтральный якорь ---
# Минимум нейтральных голосов, чтобы им доверять.
NEUTRAL_ANCHOR_MIN_VOTES = 3
# Максимальная сила подтягивания к нейтральному среднему.
NEUTRAL_ANCHOR_MAX_PULL = 0.4
# Нейтралом-якорем считаем только аккаунт с таким числом завершённых оценок других матчей.
NEUTRAL_ANCHOR_MIN_USER_HISTORY = 3

# Минимум голосов, чтобы показывать рейтинг (значение по умолчанию; см. min_votes_for_display).
MIN_VOTES_FOR_DISPLAY = 5

# Порог для бейджа «Оценок много» (между ним и MIN_VOTES_FOR_DISPLAY — «Оценок хватает»).
CONFIDENT_VOTES_THRESHOLD = 15

# Trust: минимум чужих голосов за игрока, чтобы сравнивать с консенсусом.
TRUST_MIN_COMMUNITY_VOTES = 3

# Источники флагов про пользователя: подтверждённый флаг исключает его голоса.
USER_FLAG_SOURCES = ("fast_wizard", "ip_cluster", "extreme_bias", "manual")


def min_votes_for_display() -> int:
    """Порог показа рейтинга: настройка платформы или MIN_VOTES_FOR_DISPLAY."""
    from core.models import get_setting

    return int(get_setting("min_votes_for_display", MIN_VOTES_FOR_DISPLAY))


# ---------------------------------------------------------------------------
# Публикация рейтингов матча
# ---------------------------------------------------------------------------

def published_q(prefix: str = "match__") -> Q:
    """Рейтинги матча публичны после закрытия голосования — до этого под них подстраиваются."""
    from django.utils import timezone

    return Q(**{f"{prefix}voting_open_until__lt": timezone.now()})


def ratings_hidden_for(user, match) -> bool:
    """Голосование идёт, а пользователь ещё не завершил свою оценку этого матча."""
    from django.utils import timezone

    if match.voting_open_until < timezone.now():
        return False
    if user is None or not getattr(user, "is_authenticated", False):
        return True
    return not EvaluationSession.objects.filter(user=user, match=match, status="completed").exists()


# ---------------------------------------------------------------------------
# Допустимые голоса
# ---------------------------------------------------------------------------

def excluded_voters_q(match_id) -> Q:
    """Q для исключения голосов, которые не должны влиять на рейтинг матча:
    заблокированные аккаунты, подтверждённая накрутка (по матчу или глобально),
    синтетические аккаунты в проде.
    """
    from users.models import SuspiciousActivityFlag

    confirmed_user_ids = SuspiciousActivityFlag.objects.filter(
        status="confirmed", user__isnull=False, source__in=USER_FLAG_SOURCES,
    ).filter(Q(match_id=match_id) | Q(match__isnull=True)).values("user_id")

    q = Q(user__is_active=False) | Q(user_id__in=confirmed_user_ids)
    if not getattr(settings, "COUNT_SYNTHETIC_VOTES", True):
        from core.utils import synthetic_users_q

        q |= synthetic_users_q("user__")
    return q


def countable_evaluations(queryset, match_id):
    """Оценки матча, которые идут в рейтинг: только из завершённой сессии вайзарда
    (там IP и проверка скорости) и без исключённых голосующих.
    """
    completed_user_ids = EvaluationSession.objects.filter(
        match_id=match_id, status="completed",
    ).values("user_id")
    return queryset.filter(user_id__in=completed_user_ids).exclude(excluded_voters_q(match_id))


# ---------------------------------------------------------------------------
# Команда сущности в конкретном матче
# ---------------------------------------------------------------------------

def lineup_team_subquery(match_ref: str = "match_id", player_ref: str = "player_id") -> Subquery:
    """Команда игрока в матче по заявке."""
    from lineups.models import MatchLineupPlayer

    return Subquery(
        MatchLineupPlayer.objects.filter(
            lineup__match_id=OuterRef(match_ref), player_id=OuterRef(player_ref),
        ).values("lineup__team_id")[:1]
    )


def player_team_map_for_match(match_id) -> dict:
    """{player_id: team_id} по заявке матча — не по текущему клубу игрока."""
    from lineups.models import MatchLineupPlayer

    return dict(
        MatchLineupPlayer.objects.filter(lineup__match_id=match_id)
        .values_list("player_id", "lineup__team_id")
    )


def coach_team_for_match(coach, match):
    """Команда тренера в этом матче: по home_coach/away_coach, иначе текущая."""
    if coach.id == getattr(match, "home_coach_id", None):
        return match.home_team_id
    if coach.id == getattr(match, "away_coach_id", None):
        return match.away_team_id
    return coach.team_id


# ---------------------------------------------------------------------------
# Принадлежность к лагерю
# ---------------------------------------------------------------------------

def build_allegiance(user_ids: Iterable, match) -> tuple[dict, set]:
    """({user_id: команда болельщика или None}, {id нейтралов с историей}).

    Команда — заявленная в этом матче, а если заявлен «никто» — та из команд матча,
    за которую пользователь болел в других матчах (чаще другой). Так «нейтральным»
    не становится фанат, просто не отметивший команду.
    """
    user_ids = set(user_ids)
    if not user_ids:
        return {}, set()
    match_team_ids = (match.home_team_id, match.away_team_id)

    declared = dict(
        ContextEvaluation.objects.filter(match_id=match.id, user_id__in=user_ids)
        .values_list("user_id", "supported_team_id")
    )
    supported = {uid: declared.get(uid) for uid in user_ids}

    undeclared = [uid for uid, team_id in supported.items() if team_id is None]
    if undeclared:
        history: dict = defaultdict(Counter)
        rows = (
            ContextEvaluation.objects.filter(user_id__in=undeclared, supported_team_id__in=match_team_ids)
            .exclude(match_id=match.id)
            .values("user_id", "supported_team_id")
            .annotate(n=Count("id"))
        )
        for row in rows:
            history[row["user_id"]][row["supported_team_id"]] = row["n"]
        for uid, counter in history.items():
            top = counter.most_common(2)
            if len(top) == 1 or top[0][1] > top[1][1]:
                supported[uid] = top[0][0]

    neutral_ids = [uid for uid, team_id in supported.items() if team_id is None]
    established = set()
    if neutral_ids:
        established = {
            row["user_id"]
            for row in EvaluationSession.objects.filter(user_id__in=neutral_ids, status="completed")
            .exclude(match_id=match.id)
            .values("user_id")
            .annotate(n=Count("id"))
            .filter(n__gte=NEUTRAL_ANCHOR_MIN_USER_HISTORY)
        }
    return supported, established


def effective_supported_team_id(user, match):
    """Команда болельщика для одного пользователя (см. build_allegiance)."""
    supported, _established = build_allegiance([user.id], match)
    return supported.get(user.id)


# ---------------------------------------------------------------------------
# Вес голоса и предвзятость
# ---------------------------------------------------------------------------

def calculate_user_weight(
    user: User, context_eval: ContextEvaluation | None, match=None
) -> float:
    """Вес голоса: 1.0 + 0.2 за полный просмотр + 0.2 за стадион + 0.2 за trust_score > 1.2
    минус градуированный штраф за предвзятость. Итог ограничен 0.3..2.0.
    """
    weight = 1.0
    if context_eval and context_eval.watched_type == "full":
        weight += 0.2
    if context_eval and context_eval.attended_stadium:
        weight += 0.2
    if user.trust_score > 1.2:
        weight += 0.2
    if match is not None:
        profile = _bias_profile_cached(user, match)
        penalty = _graduated_bias_penalty(profile)
        weight -= penalty
        # Порог флага калибруется; константа — запасное значение.
        from users.tasks import get_antifraud_threshold
        flag_threshold = get_antifraud_threshold("extreme_bias_flag_threshold", EXTREME_BIAS_FLAG_THRESHOLD)
        if penalty >= flag_threshold:
            _maybe_flag_extreme_bias(user, match, profile, penalty)
    return max(0.3, min(2.0, weight))


def _maybe_flag_extreme_bias(user: User, match, profile: dict, penalty: float) -> None:
    """Создаёт флаг extreme_bias при заметном штрафе. get_or_create по (user, match, source) —
    иначе дубль на каждый тип пересчёта.
    """
    from users.models import SuspiciousActivityFlag

    score = round(min(1.0, penalty / (BIAS_CONTINUOUS_MAX_PENALTY * BIAS_LOW_VARIANCE_MULTIPLIER)), 2)
    _, created = SuspiciousActivityFlag.objects.get_or_create(
        user=user, match=match, source="extreme_bias",
        defaults={
            "score": score,
            "details": {
                "mean_diff": round(profile["mean_diff"], 2) if profile.get("mean_diff") is not None else None,
                "diff_stdev": round(profile["diff_stdev"], 2) if profile.get("diff_stdev") is not None else None,
                "considered_matches": profile.get("considered"),
                "weight_penalty": round(penalty, 3),
            },
        },
    )
    if created:
        logger.info(
            "extreme_bias flagged: user=%s match=%s mean_diff=%s penalty=%.3f score=%.2f",
            user.username, match.id, profile.get("mean_diff"), penalty, score,
        )


def compute_bias_profile(
    user: User, match, lookback: int = FAN_BIAS_LOOKBACK_MATCHES
) -> dict:
    """Статистика предвзятости пользователя по матчам его команды до этого матча.

    - extreme_ratio: доля матчей со «своим» ≥9 и «чужим» ≤3;
    - mean_diff: средняя разница «свои − чужие»;
    - diff_stdev: разброс этой разницы (низкий при высоком mean_diff — похоже на накрутку).

    «Свои/чужие» — по заявке того матча, а не по текущему клубу игрока.
    considered < FAN_BIAS_MIN_HISTORY_MATCHES — истории мало, mean_diff/diff_stdev = None.
    """
    empty = {"considered": 0, "extreme_ratio": 0.0, "mean_diff": None, "diff_stdev": None}

    supported_team_id = effective_supported_team_id(user, match)
    if not supported_team_id:
        return empty

    recent_qs = match.__class__.objects.filter(
        Q(home_team_id=supported_team_id) | Q(away_team_id=supported_team_id),
        status="finished",
    )
    start_time = getattr(match, "start_time", None)
    if start_time is not None:
        # Только матчи не позже текущего: пересчёт истории не должен зависеть от будущего.
        recent_qs = recent_qs.filter(start_time__lte=start_time)
    recent_match_ids = list(recent_qs.order_by("-start_time").values_list("id", flat=True)[:lookback])

    if len(recent_match_ids) < FAN_BIAS_MIN_HISTORY_MATCHES:
        return empty

    per_match_stats = (
        PlayerEvaluation.objects.filter(user=user, match_id__in=recent_match_ids)
        .annotate(side_team_id=Coalesce(lineup_team_subquery(), F("player__team_id"), output_field=UUIDField()))
        .values("match_id")
        .annotate(
            team_avg=Avg("contribution", filter=Q(side_team_id=supported_team_id)),
            opponent_avg=Avg("contribution", filter=~Q(side_team_id=supported_team_id)),
        )
    )

    diffs: list[float] = []
    extreme_matches = 0
    for row in per_match_stats:
        if row["team_avg"] is None or row["opponent_avg"] is None:
            continue
        diffs.append(row["team_avg"] - row["opponent_avg"])
        if (
            row["team_avg"] >= FAN_BIAS_EXTREME_TEAM_SCORE
            and row["opponent_avg"] <= FAN_BIAS_EXTREME_OPPONENT_SCORE
        ):
            extreme_matches += 1

    considered = len(diffs)
    if considered < FAN_BIAS_MIN_HISTORY_MATCHES:
        return {**empty, "considered": considered}

    return {
        "considered": considered,
        "extreme_ratio": extreme_matches / considered,
        "mean_diff": sum(diffs) / considered,
        "diff_stdev": calculate_std_dev(diffs),
    }


def compute_bias_score(
    user: User, match, lookback: int = FAN_BIAS_LOOKBACK_MATCHES
) -> float | None:
    """extreme_ratio для бейджа bias_free; None — истории для вывода мало."""
    profile = compute_bias_profile(user, match, lookback)
    if profile["considered"] < FAN_BIAS_MIN_HISTORY_MATCHES:
        return None
    return profile["extreme_ratio"]


def _bias_profile_cached(user: User, match) -> dict:
    """compute_bias_profile с кэшем (не пересчитывать на каждую сущность матча)."""
    cache_key = f"fan_bias_profile:v2:{user.id}:{match.id}"
    cached_value = cache.get(cache_key)
    if cached_value is not None:
        return cached_value

    profile = compute_bias_profile(user, match)
    cache.set(cache_key, profile, timeout=FAN_BIAS_CACHE_TTL)
    return profile


def _graduated_bias_penalty(profile: dict) -> float:
    """Штраф веса, плавно растущий с mean_diff от BIAS_FREE_DIFF до BIAS_MAX_DIFF.
    Если разница почти не колеблется от матча к матчу — дополнительный множитель.
    """
    mean_diff = profile.get("mean_diff")
    if mean_diff is None or mean_diff <= BIAS_FREE_DIFF:
        return 0.0

    span = BIAS_MAX_DIFF - BIAS_FREE_DIFF
    fraction = min(1.0, (mean_diff - BIAS_FREE_DIFF) / span)
    penalty = BIAS_CONTINUOUS_MAX_PENALTY * fraction

    diff_stdev = profile.get("diff_stdev")
    if diff_stdev is not None and diff_stdev < BIAS_LOW_VARIANCE_STDEV:
        penalty *= BIAS_LOW_VARIANCE_MULTIPLIER

    return min(penalty, BIAS_CONTINUOUS_MAX_PENALTY * BIAS_LOW_VARIANCE_MULTIPLIER)


def build_user_weight_map(evaluations: list, match) -> dict[uuid.UUID, float]:
    """{user_id: вес}, один раз на матч."""
    unique_user_ids = {e.user_id for e in evaluations}

    # Контексты просмотра всех оценивших — одним запросом.
    context_map: dict[uuid.UUID, ContextEvaluation] = {
        ce.user_id: ce
        for ce in ContextEvaluation.objects.filter(
            match_id=match.id, user_id__in=unique_user_ids
        # attended_stadium нужен в calculate_user_weight — берём сразу.
        ).only("user_id", "watched_type", "attended_stadium")
    }

    weight_map: dict[uuid.UUID, float] = {}
    for eval_obj in evaluations:
        if eval_obj.user_id in weight_map:
            continue
        context = context_map.get(eval_obj.user_id)
        weight_map[eval_obj.user_id] = calculate_user_weight(eval_obj.user, context, match)

    return weight_map


# ---------------------------------------------------------------------------
# Статистика
# ---------------------------------------------------------------------------

def winsorize_values(values: list[float], pct: float = 0.1, min_n: int = 10) -> list[float]:
    """Винзоризация: крайние значения подрезаются до перцентилей pct, а не выбрасываются.
    Защищает от организованного сговора свежих аккаунтов без истории.

    :param pct: доля с каждой стороны (0.1 — 10-й/90-й перцентиль).
    :param min_n: на меньших выборках — _clip_small_sample_outliers.
    """
    n = len(values)
    if n < min_n:
        return _clip_small_sample_outliers(values)
    sorted_values = sorted(values)
    lower_idx = min(int(math.floor(n * pct)), n - 1)
    upper_idx = max(int(math.ceil(n * (1 - pct))) - 1, lower_idx)
    upper_idx = min(upper_idx, n - 1)
    lower_bound = sorted_values[lower_idx]
    upper_bound = sorted_values[upper_idx]
    return [min(max(v, lower_bound), upper_bound) for v in values]


def _clip_small_sample_outliers(values: list[float], min_n: int = 3, mad_k: float = 2.5) -> list[float]:
    """Защита на малых выборках (3-9 голосов): значения дальше mad_k MAD от медианы
    подрезаются до границы.

    :param min_n: на 1-2 голосах не делаем ничего.
    :param mad_k: мягче, чем у детектора всплесков — не искажаем мнение честной группы.
    """
    n = len(values)
    if n < min_n:
        return list(values)
    median = statistics.median(values)
    mad = statistics.median([abs(v - median) for v in values])
    if mad == 0:
        return list(values)
    scaled_mad = mad / 0.6745
    lower_bound = median - mad_k * scaled_mad
    upper_bound = median + mad_k * scaled_mad
    return [min(max(v, lower_bound), upper_bound) for v in values]


def calculate_weighted_average(
    evaluations: list,
    field_name: str,
    weight_map: dict[uuid.UUID, float],
    winsorize: bool = True,
    winsorize_pct: float = 0.1,
) -> float:
    """Взвешенное среднее по полю (по умолчанию с винзоризацией).

    :param evaluations: список, не queryset.
    :param weight_map: см. build_user_weight_map.
    """
    if not evaluations:
        return 0.0

    raw_values = [getattr(e, field_name, 0) or 0 for e in evaluations]
    values = winsorize_values(raw_values, pct=winsorize_pct) if winsorize else raw_values

    weighted_sum = 0.0
    total_weight = 0.0
    for eval_obj, value in zip(evaluations, values):
        weight = weight_map.get(eval_obj.user_id, 0.5)
        weighted_sum += value * weight
        total_weight += weight

    return weighted_sum / total_weight if total_weight > 0 else 0.0


def calculate_std_dev(values: Iterable[float]) -> float:
    """Стандартное отклонение (по генеральной совокупности, делим на n)."""
    values = list(values)
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    variance = sum((x - mean) ** 2 for x in values) / n
    return math.sqrt(variance)


def stability_index_for(values: list[float]) -> float:
    """1/σ голосов; мало голосов — 0 (не «идеальная» стабильность), σ=0 — 10."""
    if len(values) < MIN_VOTES_FOR_DISPLAY:
        return 0.0
    std_dev = calculate_std_dev(values)
    return 1.0 / std_dev if std_dev > 0 else 10.0


# ---------------------------------------------------------------------------
# Сегментация и нейтральный якорь
# ---------------------------------------------------------------------------

def segment_evaluations_by_side_multi(
    evaluations: list, value_fields: tuple[str, ...], entity_team_id, match, allegiance=None,
) -> dict[str, tuple[float | None, float | None, float | None, int, int, int]]:
    """Сегментация «свои/чужие/нейтральные» сразу для нескольких полей за один проход.

    Нейтрал без истории оценок не попадает ни в один лагерь — якорем быть не может.
    :param allegiance: результат build_allegiance (передавайте один на матч).
    :return: {поле: (own_mean, rival_mean, neutral_mean, own_n, rival_n, neutral_n)};
    пусто, если нет entity_team_id или оценок.
    """
    empty = (None, None, None, 0, 0, 0)
    if not evaluations or not entity_team_id:
        return {f: empty for f in value_fields}

    opponent_team_id = (
        match.away_team_id if match.home_team_id == entity_team_id else match.home_team_id
    )
    if allegiance is None:
        allegiance = build_allegiance({e.user_id for e in evaluations}, match)
    supported_team_map, established_neutrals = allegiance

    buckets: dict[str, dict[str, list[float]]] = {
        f: {"own": [], "rival": [], "neutral": []} for f in value_fields
    }
    for eval_obj in evaluations:
        supported_team_id = supported_team_map.get(eval_obj.user_id)
        if supported_team_id == entity_team_id:
            side = "own"
        elif supported_team_id == opponent_team_id:
            side = "rival"
        elif eval_obj.user_id in established_neutrals:
            side = "neutral"
        else:
            continue
        for field_name in value_fields:
            value = getattr(eval_obj, field_name, None)
            if value is None:
                continue
            buckets[field_name][side].append(value)

    def _mean(values: list[float]) -> float | None:
        return sum(values) / len(values) if values else None

    return {
        field_name: (
            _mean(side_values["own"]), _mean(side_values["rival"]), _mean(side_values["neutral"]),
            len(side_values["own"]), len(side_values["rival"]), len(side_values["neutral"]),
        )
        for field_name, side_values in buckets.items()
    }


def segment_evaluations_by_side(
    evaluations: list, value_field: str, entity_team_id, match, allegiance=None,
) -> tuple[float | None, float | None, float | None, int, int, int]:
    """Сегментация «свои/чужие/нейтральные» для одного поля. Средние внутри лагеря без весов."""
    return segment_evaluations_by_side_multi(
        evaluations, (value_field,), entity_team_id, match, allegiance
    )[value_field]


def apply_neutral_anchor(
    pooled_score: float,
    neutral_avg: float | None,
    own_n: int,
    rival_n: int,
    neutral_n: int,
) -> float:
    """Подтягивает итог к среднему нейтральных зрителей пропорционально доле
    фанатов обеих сторон (до NEUTRAL_ANCHOR_MAX_PULL).

    :param pooled_score: взвешенное и винзоризованное среднее.
    :param neutral_avg: среднее нейтралов с историей; мало голосов — без коррекции.
    """
    if neutral_avg is None or neutral_n < NEUTRAL_ANCHOR_MIN_VOTES:
        return pooled_score

    total = own_n + rival_n + neutral_n
    if total == 0:
        return pooled_score

    partisan_share = (own_n + rival_n) / total
    pull = NEUTRAL_ANCHOR_MAX_PULL * partisan_share
    return pooled_score * (1 - pull) + neutral_avg * pull


def recalculate_player_aggregate(player, match):
    """Агрегат игрока за матч — тот же расчёт, что в Celery-задаче (синхронно)."""
    from .models import PlayerMatchAggregate
    from .tasks import recalculate_player_aggregates

    recalculate_player_aggregates(str(match.id))
    return PlayerMatchAggregate.objects.filter(player=player, match=match).first()


# ---------------------------------------------------------------------------
# Trust score
# ---------------------------------------------------------------------------

def calculate_user_trust_adjustment(user, match) -> float:
    """Корректировка trust_score после закрытия голосования.

    RMSE по «независимым» оценкам игроков: только тем, что пользователь поставил, пока
    рейтинг игрока ещё не был публичным (чужих голосов меньше порога показа). Консенсус —
    среднее допустимых чужих голосов. Подсмотренные цифры доверия не приносят.
    """
    user_evals = list(
        PlayerEvaluation.objects.filter(user=user, match=match).values(
            "player_id", "contribution", "updated_at"
        )
    )
    if not user_evals:
        return 0.0

    player_ids = [e["player_id"] for e in user_evals]
    others = countable_evaluations(
        PlayerEvaluation.objects.filter(match=match, player_id__in=player_ids).exclude(user=user),
        match.id,
    ).values_list("player_id", "contribution", "created_at")

    by_player: dict = defaultdict(list)
    for player_id, contribution, created_at in others:
        by_player[player_id].append((contribution, created_at))

    visible_threshold = min_votes_for_display()
    squared_errors = []
    for row in user_evals:
        votes = by_player.get(row["player_id"], [])
        if len(votes) < TRUST_MIN_COMMUNITY_VOTES:
            continue
        seen_before = sum(1 for _c, created_at in votes if created_at < row["updated_at"])
        if seen_before >= visible_threshold:
            continue  # рейтинг уже был виден — оценка не независимая
        community_avg = sum(c for c, _t in votes) / len(votes)
        squared_errors.append((row["contribution"] - community_avg) ** 2)

    if not squared_errors:
        return 0.0

    rmse = math.sqrt(sum(squared_errors) / len(squared_errors))

    # Нормализация на 0..1 (RMSE 5 и больше — 1.0).
    normalized_deviation = min(rmse / 5.0, 1.0)

    if normalized_deviation < 0.3:
        return 0.05  # близок к консенсусу
    if normalized_deviation < 0.6:
        return 0.0
    return -0.05  # систематически расходится
