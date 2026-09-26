# users/services.py
"""Проверка и выдача достижений.
Вызывается только асинхронно (users/tasks.py::check_and_award_badges_task).
founder и monthly_champion выдаются отдельно.
"""
from __future__ import annotations

import logging

from django.db.models import Avg, F
from django.utils import timezone
from datetime import timedelta

from aggregates.services import compute_bias_score
from evaluations.models import ContextEvaluation, CoachEvaluation, PlayerEvaluation, RefereeEvaluation
from users.models import UserBadge

logger = logging.getLogger(__name__)

# Статусные бейджи — периодически перепроверяются (revalidate_status_badges).
STATUS_BADGE_TYPES = frozenset({"foresight", "max_trust", "stable_hand", "accurate_analyst", "bias_free"})

ACCURATE_ANALYST_LOOKBACK = 20
ACCURATE_ANALYST_MAX_DEVIATION = 1.0
ACCURATE_ANALYST_MIN_ACCURATE_RATIO = 0.8

BIAS_FREE_LOOKBACK = 15
BIAS_FREE_MAX_SCORE = 0.15  # доля матчей с экстремальным перекосом

FORESIGHT_MIN_EVALUATIONS = 30
FORESIGHT_MIN_TRUST_SCORE = 1.6

JUDGE_OF_JUDGES_MIN_COUNT = 25
POLYGLOT_MIN_TEAMS = 8

DERBY_HUNTER_MIN_MATCHES = 5

# --- Пороги новых достижений (users/badges.py) ---
COACH_EXPERT_MIN_COUNT = 25
BOTH_SIDES_MIN_MATCHES = 15
FULL_SEASON_MIN_TOURS = 10  # отсекаем короткие сезоны
SEASON_COMPLETIONIST_MIN_MATCHES = 30  # то же для «Стоглазого»
STABLE_HAND_MIN_PREDICTIONS = 50
STABLE_HAND_MIN_ACCURACY = 0.85
DERBY_PROPHET_MIN_CORRECT = 5
AGAINST_THE_TIDE_MIN_TOTAL_PREDICTIONS = 5  # минимум голосов на матче, чтобы «меньшинство» было осмысленным
PERFECT_TOUR_MIN_MATCHES = 6
MAX_TRUST_THRESHOLD = 1.95  # потолок trust_score — 2.0
MAX_TRUST_MIN_EVALUATIONS = 100


def check_and_award_badges(user) -> list[UserBadge]:
    """Проверяет условия и выдаёт достижения. Возвращает только новые UserBadge.
    Вызывать только из Celery-задачи.
    """
    awarded: list[UserBadge] = []
    total = user.total_evaluations
    streak = user.evaluation_streak

    try:
        if total >= 1:
            b, created = UserBadge.objects.get_or_create(user=user, badge_type="first_evaluation")
            if created:
                awarded.append(b)

        if total >= 10:
            b, created = UserBadge.objects.get_or_create(user=user, badge_type="active_fan_10")
            if created:
                awarded.append(b)

        if total >= 50:
            b, created = UserBadge.objects.get_or_create(user=user, badge_type="active_fan_50")
            if created:
                awarded.append(b)

        if total >= 150:
            b, created = UserBadge.objects.get_or_create(user=user, badge_type="active_fan_150")
            if created:
                awarded.append(b)

        if total >= ACCURATE_ANALYST_LOOKBACK:
            _maybe_award_accurate_analyst(user, awarded)

        if total >= BIAS_FREE_LOOKBACK:
            _maybe_award_bias_free(user, awarded)

        if total >= 5:
            early_count = ContextEvaluation.objects.filter(
                user=user,
                match__end_time__isnull=False,
                created_at__lte=F("match__end_time") + timedelta(hours=2),
            ).count()
            if early_count >= 5:
                b, created = UserBadge.objects.get_or_create(user=user, badge_type="early_bird")
                if created:
                    awarded.append(b)

        if streak >= 7:
            b, created = UserBadge.objects.get_or_create(user=user, badge_type="streak_7")
            if created:
                awarded.append(b)

        if streak >= 30:
            b, created = UserBadge.objects.get_or_create(user=user, badge_type="streak_30")
            if created:
                awarded.append(b)

        if streak >= 100:
            b, created = UserBadge.objects.get_or_create(user=user, badge_type="streak_100")
            if created:
                awarded.append(b)

        if total >= FORESIGHT_MIN_EVALUATIONS and user.trust_score >= FORESIGHT_MIN_TRUST_SCORE:
            b, created = UserBadge.objects.get_or_create(user=user, badge_type="foresight")
            if created:
                awarded.append(b)

        if RefereeEvaluation.objects.filter(user=user).count() >= JUDGE_OF_JUDGES_MIN_COUNT:
            b, created = UserBadge.objects.get_or_create(user=user, badge_type="judge_of_judges")
            if created:
                awarded.append(b)

        distinct_teams = (
            PlayerEvaluation.objects.filter(user=user)
            .values("player__team_id")
            .distinct()
            .count()
        )
        if distinct_teams >= POLYGLOT_MIN_TEAMS:
            b, created = UserBadge.objects.get_or_create(user=user, badge_type="polyglot")
            if created:
                awarded.append(b)

        if total >= DERBY_HUNTER_MIN_MATCHES:
            _maybe_award_derby_hunter(user, awarded)

        # Серии угаданных прогнозов (7/30/100) — отдельно от серий оценок.
        if user.match_predictions.exists():
            b, created = UserBadge.objects.get_or_create(user=user, badge_type="first_prediction")
            if created:
                awarded.append(b)

        prediction_streak = user.prediction_streak
        if prediction_streak >= 7:
            b, created = UserBadge.objects.get_or_create(user=user, badge_type="prediction_streak_7")
            if created:
                awarded.append(b)
        if prediction_streak >= 30:
            b, created = UserBadge.objects.get_or_create(user=user, badge_type="prediction_streak_30")
            if created:
                awarded.append(b)
        if prediction_streak >= 100:
            b, created = UserBadge.objects.get_or_create(user=user, badge_type="prediction_streak_100")
            if created:
                awarded.append(b)

        # --- Новые достижения, включая legendary ---
        if streak >= 250:
            b, created = UserBadge.objects.get_or_create(user=user, badge_type="streak_250")
            if created:
                awarded.append(b)

        if prediction_streak >= 200:
            b, created = UserBadge.objects.get_or_create(user=user, badge_type="prediction_streak_200")
            if created:
                awarded.append(b)

        if CoachEvaluation.objects.filter(user=user).count() >= COACH_EXPERT_MIN_COUNT:
            b, created = UserBadge.objects.get_or_create(user=user, badge_type="coach_expert")
            if created:
                awarded.append(b)

        if user.trust_score >= MAX_TRUST_THRESHOLD and total >= MAX_TRUST_MIN_EVALUATIONS:
            b, created = UserBadge.objects.get_or_create(user=user, badge_type="max_trust")
            if created:
                awarded.append(b)

        if total >= BOTH_SIDES_MIN_MATCHES:
            _maybe_award_both_sides(user, awarded)

        if total >= FULL_SEASON_MIN_TOURS:
            _maybe_award_full_season(user, awarded)

        if total >= SEASON_COMPLETIONIST_MIN_MATCHES:
            _maybe_award_season_completionist(user, awarded)

        if user.match_predictions.filter(match__status="finished").count() >= STABLE_HAND_MIN_PREDICTIONS:
            _maybe_award_stable_hand(user, awarded)

        if user.match_predictions.filter(match__status="finished").count() >= DERBY_PROPHET_MIN_CORRECT:
            _maybe_award_derby_prophet(user, awarded)

        # Гейт — хотя бы один угаданный прогноз; порог голосов проверяет сама функция.
        if user.match_predictions.filter(match__status="finished").exists():
            _maybe_award_against_the_tide(user, awarded)

        if user.match_predictions.filter(match__status="finished").exists():
            _maybe_award_perfect_tour(user, awarded)

    except Exception as e:
        logger.error("Ошибка проверки достижений для %s: %s", user.username, e, exc_info=True)

    return awarded


def _maybe_award_both_sides(user, awarded: list[UserBadge]) -> None:
    """«Обе стороны»: в N матчах оценены игроки обеих команд.
    Команда игрока на матч — через составы. 2 запроса без N+1.
    """
    from lineups.models import MatchLineupPlayer

    pairs = list(
        PlayerEvaluation.objects.filter(user=user)
        .values("match_id", "player_id")
        .distinct()
    )
    if not pairs:
        return

    match_ids = {r["match_id"] for r in pairs}
    player_ids = {r["player_id"] for r in pairs}
    lineup_map = {
        (mlp.player_id, mlp.lineup.match_id): mlp.lineup.team_id
        for mlp in MatchLineupPlayer.objects.filter(
            player_id__in=player_ids, lineup__match_id__in=match_ids
        ).select_related("lineup")
    }

    teams_by_match: dict[str, set] = {}
    for r in pairs:
        team_id = lineup_map.get((r["player_id"], r["match_id"]))
        if team_id is None:
            continue
        teams_by_match.setdefault(r["match_id"], set()).add(team_id)

    both_sides_count = sum(1 for teams in teams_by_match.values() if len(teams) >= 2)
    if both_sides_count >= BOTH_SIDES_MIN_MATCHES:
        b, created = UserBadge.objects.get_or_create(user=user, badge_type="both_sides")
        if created:
            awarded.append(b)


def _maybe_award_full_season(user, awarded: list[UserBadge]) -> None:
    """«Полный сезон»: хотя бы одна оценка в каждом туре сезона."""
    from matches.models import Match

    season_ids = list(
        user.evaluation_sessions.filter(status="completed", match__season__isnull=False)
        .values_list("match__season_id", flat=True)
        .distinct()
    )
    for season_id in season_ids:
        all_tours = set(
            Match.objects.filter(season_id=season_id, tour__isnull=False)
            .values_list("tour", flat=True)
            .distinct()
        )
        if len(all_tours) < FULL_SEASON_MIN_TOURS:
            continue
        evaluated_tours = set(
            user.evaluation_sessions.filter(
                status="completed", match__season_id=season_id, match__tour__isnull=False
            )
            .values_list("match__tour", flat=True)
            .distinct()
        )
        if all_tours <= evaluated_tours:
            b, created = UserBadge.objects.get_or_create(user=user, badge_type="full_season")
            if created:
                awarded.append(b)
            return


def _maybe_award_season_completionist(user, awarded: list[UserBadge]) -> None:
    """«Стоглазый» (legendary): оценены все завершённые матчи сезона."""
    from matches.models import Match

    season_ids = list(
        user.evaluation_sessions.filter(status="completed", match__season__isnull=False)
        .values_list("match__season_id", flat=True)
        .distinct()
    )
    for season_id in season_ids:
        total_matches = Match.objects.filter(season_id=season_id, status="finished").count()
        if total_matches < SEASON_COMPLETIONIST_MIN_MATCHES:
            continue
        evaluated_matches = (
            user.evaluation_sessions.filter(
                status="completed", match__season_id=season_id, match__status="finished"
            )
            .values("match_id")
            .distinct()
            .count()
        )
        if evaluated_matches >= total_matches:
            b, created = UserBadge.objects.get_or_create(user=user, badge_type="season_completionist")
            if created:
                awarded.append(b)
            return


def _maybe_award_stable_hand(user, awarded: list[UserBadge]) -> None:
    """«Стабильная рука»: >= N прогнозов с точностью >= порога.
    is_correct — property, считаем в Python.
    """
    predictions = user.match_predictions.filter(match__status="finished").select_related("match")
    total = 0
    correct = 0
    for p in predictions:
        is_correct = p.is_correct
        if is_correct is None:
            continue
        total += 1
        if is_correct:
            correct += 1
    if total >= STABLE_HAND_MIN_PREDICTIONS and correct / total >= STABLE_HAND_MIN_ACCURACY:
        b, created = UserBadge.objects.get_or_create(user=user, badge_type="stable_hand")
        if created:
            awarded.append(b)


def _maybe_award_derby_prophet(user, awarded: list[UserBadge]) -> None:
    """«Дерби-пророк»: N угаданных прогнозов на дерби."""
    from teams.models import Team

    rival_pairs: set[frozenset] = {
        frozenset((from_id, to_id))
        for from_id, to_id in Team.rivals.through.objects.values_list("from_team_id", "to_team_id")
    }
    if not rival_pairs:
        return

    predictions = user.match_predictions.filter(match__status="finished").select_related("match")
    correct_derby_count = sum(
        1
        for p in predictions
        if frozenset((p.match.home_team_id, p.match.away_team_id)) in rival_pairs and p.is_correct
    )
    if correct_derby_count >= DERBY_PROPHET_MIN_CORRECT:
        b, created = UserBadge.objects.get_or_create(user=user, badge_type="derby_prophet")
        if created:
            awarded.append(b)


def _maybe_award_against_the_tide(user, awarded: list[UserBadge]) -> None:
    """«Против течения»: угадал исход, будучи в меньшинстве голосов по матчу."""
    from django.db.models import Count

    from predictions.models import MatchPrediction

    user_correct = [
        p
        for p in user.match_predictions.filter(match__status="finished").select_related("match")
        if p.is_correct
    ]
    if not user_correct:
        return

    match_ids = [p.match_id for p in user_correct]
    counts_by_match: dict[str, dict[str, int]] = {}
    for row in (
        MatchPrediction.objects.filter(match_id__in=match_ids)
        .values("match_id", "choice")
        .annotate(c=Count("id"))
    ):
        counts_by_match.setdefault(row["match_id"], {})[row["choice"]] = row["c"]

    for p in user_correct:
        counts = counts_by_match.get(p.match_id, {})
        total_votes = sum(counts.values())
        if total_votes < AGAINST_THE_TIDE_MIN_TOTAL_PREDICTIONS:
            continue
        # Ничья за первое место — большинства нет, матч пропускаем.
        max_count = max(counts.values())
        leaders = [choice for choice, c in counts.items() if c == max_count]
        if len(leaders) != 1:
            continue
        majority_choice = leaders[0]
        if p.choice != majority_choice:
            b, created = UserBadge.objects.get_or_create(user=user, badge_type="against_the_tide")
            if created:
                awarded.append(b)
            return


def _maybe_award_perfect_tour(user, awarded: list[UserBadge]) -> None:
    """«Идеальный тур» (legendary): спрогнозированы и угаданы все матчи тура."""
    from matches.models import Match

    tour_season_pairs = (
        user.match_predictions.filter(match__tour__isnull=False)
        .values_list("match__season_id", "match__tour")
        .distinct()
    )
    for season_id, tour in tour_season_pairs:
        tour_matches = list(Match.objects.filter(season_id=season_id, tour=tour, status="finished"))
        if len(tour_matches) < PERFECT_TOUR_MIN_MATCHES:
            continue
        tour_match_ids = {m.id for m in tour_matches}
        user_predictions = {
            p.match_id: p
            for p in user.match_predictions.filter(match_id__in=tour_match_ids).select_related("match")
        }
        if set(user_predictions.keys()) != tour_match_ids:
            continue  # спрогнозированы не все матчи тура
        if all(p.is_correct for p in user_predictions.values()):
            b, created = UserBadge.objects.get_or_create(user=user, badge_type="perfect_tour")
            if created:
                awarded.append(b)
            return


def _maybe_award_accurate_analyst(user, awarded: list[UserBadge]) -> None:
    """«Точный аналитик»: отклонение <= 1.0 от среднего сообщества в >= 80% из последних 20 матчей."""
    recent_match_ids = list(
        ContextEvaluation.objects.filter(user=user, match__isnull=False)
        .order_by("-created_at")
        .values_list("match_id", flat=True)[:ACCURATE_ANALYST_LOOKBACK]
    )
    if not recent_match_ids:
        return

    user_avg_by_match = dict(
        PlayerEvaluation.objects.filter(user=user, match_id__in=recent_match_ids)
        .values("match_id")
        .annotate(avg=Avg("contribution"))
        .values_list("match_id", "avg")
    )
    community_avg_by_match = dict(
        PlayerEvaluation.objects.filter(match_id__in=recent_match_ids)
        .exclude(user=user)
        .values("match_id")
        .annotate(avg=Avg("contribution"))
        .values_list("match_id", "avg")
    )

    accurate = sum(
        1
        for match_id in recent_match_ids
        if match_id in user_avg_by_match
        and match_id in community_avg_by_match
        and abs(user_avg_by_match[match_id] - community_avg_by_match[match_id]) <= ACCURATE_ANALYST_MAX_DEVIATION
    )

    if accurate >= len(recent_match_ids) * ACCURATE_ANALYST_MIN_ACCURATE_RATIO:
        b, created = UserBadge.objects.get_or_create(user=user, badge_type="accurate_analyst")
        if created:
            awarded.append(b)


def _maybe_award_bias_free(user, awarded: list[UserBadge]) -> None:
    """«Без предвзятости»: compute_bias_score по матчам своей команды ниже порога."""
    latest_context = (
        ContextEvaluation.objects.filter(user=user, supported_team__isnull=False, match__isnull=False)
        .select_related("match")
        .order_by("-created_at")
        .first()
    )
    if not latest_context or not latest_context.match_id:
        return

    bias_score = compute_bias_score(user, latest_context.match, lookback=BIAS_FREE_LOOKBACK)
    # None — истории мало: «без предвзятости» ещё нечем подтвердить.
    if bias_score is not None and bias_score <= BIAS_FREE_MAX_SCORE:
        b, created = UserBadge.objects.get_or_create(user=user, badge_type="bias_free")
        if created:
            awarded.append(b)


def _maybe_award_derby_hunter(user, awarded: list[UserBadge]) -> None:
    """«Дерби-эксперт»: N оценённых матчей между соперниками (Team.rivals)."""
    from matches.models import Match
    from teams.models import Team

    rival_pairs: set[frozenset] = {
        frozenset((from_id, to_id))
        for from_id, to_id in Team.rivals.through.objects.values_list("from_team_id", "to_team_id")
    }
    if not rival_pairs:
        return

    evaluated_matches = (
        Match.objects.filter(context_evaluations__user=user)
        .values_list("id", "home_team_id", "away_team_id")
        .distinct()
    )

    derby_count = sum(
        1
        for _match_id, home_id, away_id in evaluated_matches
        if frozenset((home_id, away_id)) in rival_pairs
    )

    if derby_count >= DERBY_HUNTER_MIN_MATCHES:
        b, created = UserBadge.objects.get_or_create(user=user, badge_type="derby_hunter")
        if created:
            awarded.append(b)


# --- Переоценка статусных бейджей ---
# Те же условия, что в _maybe_award_*, но без выдачи — только bool.

def _check_foresight_condition(user) -> bool:
    return user.total_evaluations >= FORESIGHT_MIN_EVALUATIONS and user.trust_score >= FORESIGHT_MIN_TRUST_SCORE


def _check_max_trust_condition(user) -> bool:
    return user.trust_score >= MAX_TRUST_THRESHOLD and user.total_evaluations >= MAX_TRUST_MIN_EVALUATIONS


def _check_stable_hand_condition(user) -> bool:
    predictions = user.match_predictions.filter(match__status="finished").select_related("match")
    total = 0
    correct = 0
    for p in predictions:
        is_correct = p.is_correct
        if is_correct is None:
            continue
        total += 1
        if is_correct:
            correct += 1
    return total >= STABLE_HAND_MIN_PREDICTIONS and correct / total >= STABLE_HAND_MIN_ACCURACY


def _check_accurate_analyst_condition(user) -> bool:
    recent_match_ids = list(
        ContextEvaluation.objects.filter(user=user, match__isnull=False)
        .order_by("-created_at")
        .values_list("match_id", flat=True)[:ACCURATE_ANALYST_LOOKBACK]
    )
    if not recent_match_ids:
        return False

    user_avg_by_match = dict(
        PlayerEvaluation.objects.filter(user=user, match_id__in=recent_match_ids)
        .values("match_id")
        .annotate(avg=Avg("contribution"))
        .values_list("match_id", "avg")
    )
    community_avg_by_match = dict(
        PlayerEvaluation.objects.filter(match_id__in=recent_match_ids)
        .exclude(user=user)
        .values("match_id")
        .annotate(avg=Avg("contribution"))
        .values_list("match_id", "avg")
    )
    accurate = sum(
        1
        for match_id in recent_match_ids
        if match_id in user_avg_by_match
        and match_id in community_avg_by_match
        and abs(user_avg_by_match[match_id] - community_avg_by_match[match_id]) <= ACCURATE_ANALYST_MAX_DEVIATION
    )
    return accurate >= len(recent_match_ids) * ACCURATE_ANALYST_MIN_ACCURATE_RATIO


def _check_bias_free_condition(user) -> bool:
    latest_context = (
        ContextEvaluation.objects.filter(user=user, supported_team__isnull=False, match__isnull=False)
        .select_related("match")
        .order_by("-created_at")
        .first()
    )
    if not latest_context or not latest_context.match_id:
        return False
    bias_score = compute_bias_score(user, latest_context.match, lookback=BIAS_FREE_LOOKBACK)
    return bias_score is not None and bias_score <= BIAS_FREE_MAX_SCORE


_STATUS_BADGE_CHECKS = {
    "foresight": _check_foresight_condition,
    "max_trust": _check_max_trust_condition,
    "stable_hand": _check_stable_hand_condition,
    "accurate_analyst": _check_accurate_analyst_condition,
    "bias_free": _check_bias_free_condition,
}


def revalidate_status_badges(user) -> dict[str, list[str]]:
    """Ставит/снимает is_stale у уже выданных статусных бейджей. Новые не выдаёт."""
    existing = {
        b.badge_type: b
        for b in UserBadge.objects.filter(user=user, badge_type__in=STATUS_BADGE_TYPES)
    }
    now_stale: list[str] = []
    reactivated: list[str] = []

    for badge_type, badge in existing.items():
        check_fn = _STATUS_BADGE_CHECKS[badge_type]
        try:
            holds = check_fn(user)
        except Exception as e:
            logger.error(
                "revalidate_status_badges: ошибка проверки %s для %s: %s",
                badge_type, user.username, e, exc_info=True,
            )
            continue

        if not holds and not badge.is_stale:
            badge.is_stale = True
            badge.stale_since = timezone.now()
            badge.save(update_fields=["is_stale", "stale_since"])
            now_stale.append(badge_type)
        elif holds and badge.is_stale:
            badge.is_stale = False
            badge.stale_since = None
            badge.save(update_fields=["is_stale", "stale_since"])
            reactivated.append(badge_type)

    return {"now_stale": now_stale, "reactivated": reactivated}