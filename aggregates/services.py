# aggregates/services.py
"""Формула агрегатов (игроки, тренеры, команды, судьи) и защита оценок.

Вес голоса (build_user_weight_map) считается один раз на матч и зависит от
просмотра, trust_score и истории предвзятости пользователя.
Дополнительные слои защиты:
- винзоризация хвостов (а для 3-9 голосов — клиппинг по медиане/MAD);
- градуированный штраф веса за систематическую предвзятость к своей команде;
- нейтральный якорь: при большой доле фанатов обеих сторон итог
  подтягивается к мнению нейтральных зрителей.
"""
from __future__ import annotations

import logging
import math
import statistics
import uuid
from typing import Iterable

from django.core.cache import cache
from django.db.models import Avg, ExpressionWrapper, F, FloatField, Q, Sum
from django.db.models.functions import NullIf

from evaluations.models import ContextEvaluation, PlayerEvaluation
from users.models import User

logger = logging.getLogger(__name__)


def vote_weighted_avg(field: str, votes_field: str = "total_votes") -> ExpressionWrapper:
    """Среднее по матчам с учётом числа голосов: Σ(значение × голосов) / Σ(голосов).

    Для полей через связь передавайте votes_field с тем же префиксом.
    Нет голосов — None. Не объявляйте в том же запросе алиас total_votes раньше
    этого выражения — Django примет его за агрегат и упадёт.
    """
    return ExpressionWrapper(
        Sum(F(field) * F(votes_field), output_field=FloatField()) / NullIf(Sum(votes_field), 0),
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

# Минимум голосов, чтобы показывать рейтинг.
MIN_VOTES_FOR_DISPLAY = 5

# Порог для бейджа «Оценок много» (между ним и MIN_VOTES_FOR_DISPLAY — «Оценок хватает»).
CONFIDENT_VOTES_THRESHOLD = 15


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
    """Статистика предвзятости пользователя по последним матчам его команды.

    - extreme_ratio: доля матчей со «своим» ≥9 и «чужим» ≤3 (старый бинарный сигнал);
    - mean_diff: средняя разница «свои − чужие»;
    - diff_stdev: разброс этой разницы (низкий при высоком mean_diff — похоже на накрутку).

    mean_diff/diff_stdev = None, если истории мало.
    """
    empty = {"considered": 0, "extreme_ratio": 0.0, "mean_diff": None, "diff_stdev": None}

    context = (
        ContextEvaluation.objects.filter(user=user, match=match)
        .only("supported_team_id")
        .first()
    )
    supported_team_id = context.supported_team_id if context else None
    if not supported_team_id:
        return empty

    recent_match_ids = list(
        match.__class__.objects.filter(
            Q(home_team_id=supported_team_id) | Q(away_team_id=supported_team_id),
            status="finished",
        )
        .order_by("-start_time")
        .values_list("id", flat=True)[:lookback]
    )

    if len(recent_match_ids) < FAN_BIAS_MIN_HISTORY_MATCHES:
        return empty

    per_match_stats = (
        PlayerEvaluation.objects.filter(user=user, match_id__in=recent_match_ids)
        .values("match_id")
        .annotate(
            team_avg=Avg("contribution", filter=Q(player__team_id=supported_team_id)),
            opponent_avg=Avg(
                "contribution", filter=~Q(player__team_id=supported_team_id)
            ),
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
        return empty

    return {
        "considered": considered,
        "extreme_ratio": extreme_matches / considered,
        "mean_diff": sum(diffs) / considered,
        "diff_stdev": calculate_std_dev(diffs),
    }


def compute_bias_score(
    user: User, match, lookback: int = FAN_BIAS_LOOKBACK_MATCHES
) -> float:
    """Только extreme_ratio (для бейджа bias_free)."""
    return compute_bias_profile(user, match, lookback)["extreme_ratio"]


def _bias_profile_cached(user: User, match) -> dict:
    """compute_bias_profile с кэшем (не пересчитывать на каждую сущность матча)."""
    cache_key = f"fan_bias_profile:{user.id}:{match.id}"
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


def build_user_weight_map(evaluations: list[PlayerEvaluation], match) -> dict[uuid.UUID, float]:
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
    seen_users: set[uuid.UUID] = set()
    for eval_obj in evaluations:
        if eval_obj.user_id in seen_users:
            continue
        seen_users.add(eval_obj.user_id)
        context = context_map.get(eval_obj.user_id)
        weight_map[eval_obj.user_id] = calculate_user_weight(eval_obj.user, context, match)

    return weight_map


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
    evaluations: list[PlayerEvaluation],
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
    """Стандартное отклонение выборки."""
    values = list(values)
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    variance = sum((x - mean) ** 2 for x in values) / n
    return math.sqrt(variance)


def segment_evaluations_by_side_multi(
    evaluations: list, value_fields: tuple[str, ...], entity_team_id, match
) -> dict[str, tuple[float | None, float | None, float | None, int, int, int]]:
    """Сегментация «свои/чужие/нейтральные» сразу для нескольких полей за один проход.

    :return: {поле: (own_mean, rival_mean, neutral_mean, own_n, rival_n, neutral_n)};
    пусто, если нет entity_team_id или оценок.
    """
    empty = (None, None, None, 0, 0, 0)
    if not evaluations or not entity_team_id:
        return {f: empty for f in value_fields}

    opponent_team_id = (
        match.away_team_id if match.home_team_id == entity_team_id else match.home_team_id
    )

    user_ids = {e.user_id for e in evaluations}
    supported_team_map: dict[uuid.UUID, uuid.UUID | None] = {
        ce["user_id"]: ce["supported_team_id"]
        for ce in ContextEvaluation.objects.filter(
            match_id=match.id, user_id__in=user_ids
        ).values("user_id", "supported_team_id")
    }

    buckets: dict[str, dict[str, list[float]]] = {
        f: {"own": [], "rival": [], "neutral": []} for f in value_fields
    }
    for eval_obj in evaluations:
        supported_team_id = supported_team_map.get(eval_obj.user_id)
        if supported_team_id == entity_team_id:
            side = "own"
        elif supported_team_id == opponent_team_id:
            side = "rival"
        else:
            side = "neutral"
        for field_name in value_fields:
            value = getattr(eval_obj, field_name, None)
            if not value:
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
    evaluations: list, value_field: str, entity_team_id, match
) -> tuple[float | None, float | None, float | None, int, int, int]:
    """Сегментация «свои/чужие/нейтральные» для одного поля.
    Средние внутри лагеря без весов.

    :param entity_team_id: None — сегментация невозможна.
    :return: (own_mean, rival_mean, neutral_mean, own_n, rival_n, neutral_n)
    """
    return segment_evaluations_by_side_multi(evaluations, (value_field,), entity_team_id, match)[
        value_field
    ]


def _segment_by_fan_side(
    evaluations: list[PlayerEvaluation], player, match
) -> tuple[float | None, float | None, float | None, int, int, int]:
    """Обёртка для игрока (совместимость с recalculate_player_aggregate и тестами)."""
    return segment_evaluations_by_side(evaluations, "contribution", player.team_id, match)


def apply_neutral_anchor(
    pooled_score: float,
    neutral_avg: float | None,
    own_n: int,
    rival_n: int,
    neutral_n: int,
) -> float:
    """Подтягивает итог к среднему нейтральных зрителей пропорционально доле
    фанатов обеих сторон (до NEUTRAL_ANCHOR_MAX_PULL). Не требует истории
    пользователей и одинаково гасит перекос в обе стороны.

    :param pooled_score: взвешенное и винзоризованное среднее.
    :param neutral_avg: среднее нейтральных; мало голосов — без коррекции.
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
    """Пересчёт агрегата игрока за матч (синхронная версия, используется в тестах)."""
    from .models import MatchAggregate, PlayerMatchAggregate

    match_id = str(match.id)

    evaluations = list(
        PlayerEvaluation.objects.filter(player=player, match=match).select_related("user")
    )
    if not evaluations:
        return None

    weight_map = build_user_weight_map(evaluations, match)

    avg_contribution = calculate_weighted_average(evaluations, "contribution", weight_map)
    avg_risk = calculate_weighted_average(evaluations, "risk", weight_map)
    avg_potential = calculate_weighted_average(evaluations, "potential", weight_map)

    # contribution и risk сегментируем за один проход.
    segments = segment_evaluations_by_side_multi(
        evaluations, ("contribution", "risk"), player.team_id, match
    )
    own_fans_avg, rival_fans_avg, neutral_avg, own_n, rival_n, neutral_n = segments["contribution"]
    _, _, neutral_risk_avg, risk_own_n, risk_rival_n, risk_neutral_n = segments["risk"]

    contributions = [e.contribution for e in evaluations if e.contribution]
    std_dev = calculate_std_dev(contributions)
    # Мало голосов — стабильность 0, а не «идеальная».
    if len(evaluations) < MIN_VOTES_FOR_DISPLAY:
        stability_index = 0.0
    else:
        stability_index = 1.0 / std_dev if std_dev > 0 else 10.0

    drama_index = cache.get(f"match_agg_{match_id}")
    if drama_index is None:
        match_agg = MatchAggregate.objects.filter(match=match).only("drama_index").first()
        # Fallback drama_index — середина шкалы 0..100.
        drama_index = match_agg.drama_index if match_agg else 50.0
        cache.set(f"match_agg_{match_id}", drama_index, 600)

    # performance_score и risk_index подтянуты к якорю; avg_* — без якоря.
    performance_score = apply_neutral_anchor(avg_contribution, neutral_avg, own_n, rival_n, neutral_n)
    risk_index_value = apply_neutral_anchor(
        avg_risk, neutral_risk_avg, risk_own_n, risk_rival_n, risk_neutral_n
    )
    maturity_score = performance_score - risk_index_value
    # drama_index в шкале 0..100 — делим на 100.
    clutch_index = performance_score * (drama_index / 100.0)

    aggregate, _created = PlayerMatchAggregate.objects.update_or_create(
        player=player,
        match=match,
        defaults={
            "avg_contribution": round(avg_contribution, 2),
            "avg_risk": round(avg_risk, 2),
            "avg_potential": round(avg_potential, 2),
            "total_votes": len(evaluations),
            "performance_score": round(performance_score, 2),
            "risk_index": round(risk_index_value, 2),
            "maturity_score": round(maturity_score, 2),
            "stability_index": round(stability_index, 2),
            "clutch_index": round(clutch_index, 2),
            "own_fans_avg": round(own_fans_avg, 2) if own_fans_avg is not None else None,
            "rival_fans_avg": round(rival_fans_avg, 2) if rival_fans_avg is not None else None,
            "neutral_avg": round(neutral_avg, 2) if neutral_avg is not None else None,
        },
    )

    cache.set(
        f"player_agg_{player.id}_{match_id}",
        {
            "id": str(aggregate.id),
            "performance_score": aggregate.performance_score,
            "total_votes": aggregate.total_votes,
        },
        300,
    )

    return aggregate


def calculate_user_trust_adjustment(user, match) -> float:
    """Корректировка trust_score по точности оценок (RMSE от сообщества по каждому игроку)."""
    user_evals = list(
        PlayerEvaluation.objects.filter(user=user, match=match).values(
            "player_id", "contribution"
        )
    )
    if not user_evals:
        return 0.0

    player_ids = [e["player_id"] for e in user_evals]

    # Без самого пользователя.
    community_avg_by_player: dict[uuid.UUID, float] = {
        row["player_id"]: row["avg"]
        for row in PlayerEvaluation.objects.filter(match=match, player_id__in=player_ids)
        .exclude(user=user)
        .values("player_id")
        .annotate(avg=Avg("contribution"))
    }

    squared_errors = []
    for row in user_evals:
        community_avg = community_avg_by_player.get(row["player_id"])
        if community_avg is None:
            continue  # единственный оценивший
        squared_errors.append((row["contribution"] - community_avg) ** 2)

    if not squared_errors:
        return 0.0

    rmse = math.sqrt(sum(squared_errors) / len(squared_errors))

    # Нормализация на 0..1 (максимальная ошибка — 9).
    normalized_deviation = min(rmse / 5.0, 1.0)

    if normalized_deviation < 0.3:
        return 0.05  # близок к консенсусу
    if normalized_deviation < 0.6:
        return 0.0
    return -0.05  # систематически расходится


def detect_fan_bias(user, match, supported_team=None) -> dict:
    """Предвзятость в одном матче (для модерации)."""
    if not supported_team:
        context = ContextEvaluation.objects.filter(user=user, match=match).first()
        supported_team = context.supported_team if context else None

    if not supported_team:
        return {"is_biased": False, "score": 0.0}

    own_team_evals = (
        PlayerEvaluation.objects.filter(
            user=user, match=match, player__team=supported_team
        ).aggregate(avg=Avg("contribution"))["avg"]
        or 0
    )

    opponent_team = (
        match.away_team if match.home_team_id == supported_team.id else match.home_team
    )
    opponent_evals = (
        PlayerEvaluation.objects.filter(
            user=user, match=match, player__team=opponent_team
        ).aggregate(avg=Avg("contribution"))["avg"]
        or 0
    )

    bias_score = own_team_evals - opponent_evals
    is_biased = bias_score > 4.0

    return {
        "is_biased": is_biased,
        "score": bias_score,
        "own_team_avg": own_team_evals,
        "opponent_avg": opponent_evals,
    }