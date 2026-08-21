# aggregates/services.py
"""
Взвешенные агрегаты игроков/матчей + fan-bias для веса голоса и trust_score.

calculate_user_weight/_build_user_weight_map считают вес пользователя один
раз на матч и переиспользуют — вес не зависит от того, какое поле (contribution/
risk/potential) усредняется. compute_bias_score — единственный источник
исторического bias-score в проекте (использует и вес голоса, и бейдж bias_free
в users/services.py, каждый со своим порогом интерпретации поверх одного числа).
calculate_user_trust_adjustment считает RMSE отклонений от сообщества
поигроково, а не по среднему за весь матч — усреднение по матчу маскирует
предвзятость (10/10 своим + 1/10 чужим даёт околонулевую разницу средних).
"""
from __future__ import annotations

import logging
import math
import uuid
from typing import Iterable

from django.core.cache import cache
from django.db.models import Avg, Q

from evaluations.models import ContextEvaluation, PlayerEvaluation
from users.models import User

logger = logging.getLogger(__name__)

FAN_BIAS_CACHE_TTL = 600  # секунд; fan-bias не меняется чаще, чем раз в 10 минут
FAN_BIAS_MIN_HISTORY_MATCHES = 3
FAN_BIAS_LOOKBACK_MATCHES = 10
FAN_BIAS_EXTREME_TEAM_SCORE = 9
FAN_BIAS_EXTREME_OPPONENT_SCORE = 3
FAN_BIAS_THRESHOLD_RATIO = 0.7

# Порог голосов, ниже которого рейтинг игрока не считается статистически
# представительным. Используется и для фильтрации топов (matches/views.py,
# teams/views.py), и для отображения ("Недостаточно данных" —
# core/templatetags/rating_extras.py, импортирует это же значение).
MIN_VOTES_FOR_DISPLAY = 5

# Второй, более высокий порог для градуированного бейджа доверия (продуктовый
# аудит "доверие к рейтингу", 2026-08-21): между MIN_VOTES_FOR_DISPLAY и этим
# числом рейтинг уже показывается как число, но помечается "Есть данные", а
# не "Высокая надёжность" — 5 голосов статистически достаточно, чтобы не
# считаться шумом одного тролля, но недостаточно, чтобы считаться устоявшимся
# консенсусом. Используется только для UI-бейджа (core/templatetags/rating_extras.py
# ::confidence_badge), НЕ влияет на то, показывается ли число вообще —
# за это по-прежнему отвечает MIN_VOTES_FOR_DISPLAY.
CONFIDENT_VOTES_THRESHOLD = 15


def calculate_user_weight(
    user: User, context_eval: ContextEvaluation | None, match=None
) -> float:
    """Вес голоса для взвешенного среднего: +0.2 за полный просмотр, +0.2 за trust_score, -0.3 при fan-bias."""
    weight = 1.0
    if context_eval and context_eval.watched_type == "full":
        weight += 0.2
    if user.trust_score > 1.2:
        weight += 0.2
    if match is not None and _detect_fan_bias_cached(user, match):
        weight -= 0.3
    return max(0.5, min(2.0, weight))


def compute_bias_score(
    user: User, match, lookback: int = FAN_BIAS_LOOKBACK_MATCHES
) -> float:
    """
    Доля последних `lookback` матчей поддерживаемой команды, где пользователь
    поставил своей команде экстремально высокую, а сопернику — экстремально
    низкую оценку. `0.0` при недостатке истории — не значит "не предвзят",
    просто нет данных; порог интерпретации задаёт вызывающий код.

    Один запрос с group by match_id и условными Avg(filter=...) вместо
    двух запросов на каждый из lookback матчей.
    """
    context = (
        ContextEvaluation.objects.filter(user=user, match=match)
        .only("supported_team_id")
        .first()
    )
    supported_team_id = context.supported_team_id if context else None
    if not supported_team_id:
        return 0.0

    recent_match_ids = list(
        match.__class__.objects.filter(
            Q(home_team_id=supported_team_id) | Q(away_team_id=supported_team_id),
            status="finished",
        )
        .order_by("-start_time")
        .values_list("id", flat=True)[:lookback]
    )

    if len(recent_match_ids) < FAN_BIAS_MIN_HISTORY_MATCHES:
        return 0.0

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

    considered = 0
    extreme_matches = 0
    for row in per_match_stats:
        if row["team_avg"] is None or row["opponent_avg"] is None:
            continue
        considered += 1
        if (
            row["team_avg"] >= FAN_BIAS_EXTREME_TEAM_SCORE
            and row["opponent_avg"] <= FAN_BIAS_EXTREME_OPPONENT_SCORE
        ):
            extreme_matches += 1

    if considered < FAN_BIAS_MIN_HISTORY_MATCHES:
        return 0.0

    return extreme_matches / considered


def _detect_fan_bias_cached(user: User, match) -> bool:
    """compute_bias_score с кэшем на FAN_BIAS_CACHE_TTL — не пересчитывать на каждого игрока матча."""
    cache_key = f"fan_bias:{user.id}:{match.id}"
    cached_value = cache.get(cache_key)
    if cached_value is not None:
        return cached_value

    result = compute_bias_score(user, match) >= FAN_BIAS_THRESHOLD_RATIO
    cache.set(cache_key, result, timeout=FAN_BIAS_CACHE_TTL)
    return result


def _build_user_weight_map(evaluations: list[PlayerEvaluation], match) -> dict[uuid.UUID, float]:
    """Карта {user_id: вес}, посчитанная один раз на матч — вес не зависит от усредняемого поля."""
    unique_user_ids = {e.user_id for e in evaluations}

    # Контексты просмотра всех зрителей — одним IN-запросом, не по одному на пользователя.
    context_map: dict[uuid.UUID, ContextEvaluation] = {
        ce.user_id: ce
        for ce in ContextEvaluation.objects.filter(
            match_id=match.id, user_id__in=unique_user_ids
        ).only("user_id", "watched_type")
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


def calculate_weighted_average(
    evaluations: list[PlayerEvaluation],
    field_name: str,
    weight_map: dict[uuid.UUID, float],
) -> float:
    """
    Взвешенное среднее по полю.

    :param evaluations: материализованный список, не queryset — иначе SQL
        переисполняется на каждый вызов (contribution/risk/potential — 3 раза).
    :param weight_map: см. `_build_user_weight_map`.
    """
    if not evaluations:
        return 0.0

    weighted_sum = 0.0
    total_weight = 0.0
    for eval_obj in evaluations:
        weight = weight_map.get(eval_obj.user_id, 0.5)
        value = getattr(eval_obj, field_name, 0) or 0
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


def _segment_by_fan_side(
    evaluations: list[PlayerEvaluation], player, match
) -> tuple[float | None, float | None, float | None]:
    """
    Делит contribution по лагерю зрителя относительно команды игрока: свои /
    чужие / нейтральные. Read-only разрез, не влияет на performance_score.
    Простое (невзвешенное) среднее — задача показать разброс мнений, а не
    точность итога; вес пользователя тут сместил бы интерпретацию.
    """
    if not evaluations:
        return None, None, None

    player_team_id = player.team_id
    opponent_team_id = (
        match.away_team_id if match.home_team_id == player_team_id else match.home_team_id
    )

    user_ids = {e.user_id for e in evaluations}
    supported_team_map: dict[uuid.UUID, uuid.UUID | None] = {
        ce["user_id"]: ce["supported_team_id"]
        for ce in ContextEvaluation.objects.filter(
            match_id=match.id, user_id__in=user_ids
        ).values("user_id", "supported_team_id")
    }

    own_values: list[float] = []
    rival_values: list[float] = []
    neutral_values: list[float] = []
    for eval_obj in evaluations:
        if not eval_obj.contribution:
            continue
        supported_team_id = supported_team_map.get(eval_obj.user_id)
        if supported_team_id == player_team_id:
            own_values.append(eval_obj.contribution)
        elif supported_team_id == opponent_team_id:
            rival_values.append(eval_obj.contribution)
        else:
            neutral_values.append(eval_obj.contribution)

    def _mean(values: list[float]) -> float | None:
        return sum(values) / len(values) if values else None

    return _mean(own_values), _mean(rival_values), _mean(neutral_values)


def recalculate_player_aggregate(player, match):
    """Пересчёт агрегатов игрока за матч с учётом весов. Всегда возвращает PlayerMatchAggregate (или None без оценок)."""
    from .models import MatchAggregate, PlayerMatchAggregate

    match_id = str(match.id)

    evaluations = list(
        PlayerEvaluation.objects.filter(player=player, match=match).select_related("user")
    )
    if not evaluations:
        return None

    weight_map = _build_user_weight_map(evaluations, match)

    avg_contribution = calculate_weighted_average(evaluations, "contribution", weight_map)
    avg_risk = calculate_weighted_average(evaluations, "risk", weight_map)
    avg_potential = calculate_weighted_average(evaluations, "potential", weight_map)

    own_fans_avg, rival_fans_avg, neutral_avg = _segment_by_fan_side(evaluations, player, match)

    contributions = [e.contribution for e in evaluations if e.contribution]
    std_dev = calculate_std_dev(contributions)
    stability_index = 1.0 / std_dev if std_dev > 0 else 10.0

    drama_index = cache.get(f"match_agg_{match_id}")
    if drama_index is None:
        match_agg = MatchAggregate.objects.filter(match=match).only("drama_index").first()
        drama_index = match_agg.drama_index if match_agg else 5.0
        cache.set(f"match_agg_{match_id}", drama_index, 600)

    performance_score = avg_contribution
    maturity_score = avg_contribution - avg_risk
    clutch_index = avg_contribution * (drama_index / 10.0)

    aggregate, _created = PlayerMatchAggregate.objects.update_or_create(
        player=player,
        match=match,
        defaults={
            "avg_contribution": round(avg_contribution, 2),
            "avg_risk": round(avg_risk, 2),
            "avg_potential": round(avg_potential, 2),
            "total_votes": len(evaluations),
            "performance_score": round(performance_score, 2),
            "risk_index": round(avg_risk, 2),
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
    """
    Корректировка trust_score по точности оценок. RMSE отклонений от среднего
    сообщества считается поигроково и агрегируется — усреднение по всему
    матчу маскирует предвзятость (10/10 своим + 1/10 чужим даёт околонулевую
    разницу средних, хотя каждая оценка предвзята).
    """
    user_evals = list(
        PlayerEvaluation.objects.filter(user=user, match=match).values(
            "player_id", "contribution"
        )
    )
    if not user_evals:
        return 0.0

    player_ids = [e["player_id"] for e in user_evals]

    # exclude(user=user) — иначе пользователь частично сравнивается сам с собой.
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
            continue  # пользователь — единственный оценивший этого игрока
        squared_errors.append((row["contribution"] - community_avg) ** 2)

    if not squared_errors:
        return 0.0

    rmse = math.sqrt(sum(squared_errors) / len(squared_errors))

    # Нормализация на шкалу 0..1 (максимально возможная ошибка на шкале 1..10 — 9)
    normalized_deviation = min(rmse / 5.0, 1.0)

    if normalized_deviation < 0.3:
        return 0.05  # Адекватный аналитик — стабильно близок к консенсусу по каждому игроку
    if normalized_deviation < 0.6:
        return 0.0  # Норма
    return -0.05  # Систематически предвзят по отдельным игрокам


def detect_fan_bias(user, match, supported_team=None) -> dict:
    """Снимок предвзятости по одному матчу (админка/модерация) — в отличие от compute_bias_score, которая историческая."""
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