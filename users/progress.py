# users/progress.py
"""Пересчёт прогресса пользователя из фактических данных: счётчик и серия оценок,
серия прогнозов, XP/уровень, достижения. Нужен, когда сессии или прогнозы удалили —
накопленные значения иначе остаются старыми.
"""
from __future__ import annotations

import logging
import math

from django.db import transaction

logger = logging.getLogger(__name__)

# Достижения, которые не выводятся из оценок/прогнозов — пересчёт их не трогает.
PRESERVED_BADGES = frozenset({"founder"})


def evaluation_xp(session, lineup_total: int, rated_players: int) -> float:
    """Базовый XP за завершённую оценку — те же правила, что у шагов вайзарда."""
    from evaluations.views import (
        XP_COACHES_STEP, XP_CONTEXT_STEP, XP_FINAL_STEP, XP_PLAYERS_STEP_MAX, XP_REFEREE_STEP, XP_TEAMS_STEP,
    )

    players = XP_PLAYERS_STEP_MAX * min(1.0, rated_players / lineup_total) if lineup_total else 0.0
    return XP_CONTEXT_STEP + XP_TEAMS_STEP + players + XP_COACHES_STEP + XP_REFEREE_STEP + XP_FINAL_STEP


def recompute_user_progress(user) -> dict:
    """Пересобирает прогресс с нуля. Возвращает сводку изменений (для логов и команды)."""
    from django.db.models import Count

    from evaluations.models import EvaluationSession, PlayerEvaluation
    from lineups.models import MatchLineupPlayer
    from predictions.models import MatchPrediction
    from users.models import User, UserBadge, UserXP, level_for_total_xp

    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=user.pk)
        before = {
            "total_evaluations": user.total_evaluations, "evaluation_streak": user.evaluation_streak,
            "prediction_streak": user.prediction_streak,
        }

        # 1. Оценки: счётчик и серия по турам — воспроизводим по порядку завершения.
        sessions = list(
            EvaluationSession.objects.filter(user=user, status="completed")
            .select_related("match").order_by("completed_at", "created_at")
        )
        user.total_evaluations = 0
        user.evaluation_streak = 0
        user.last_evaluation_season_id = None
        user.last_evaluation_tour = None
        peak_eval_streak = 0
        for session in sessions:
            user.apply_evaluation_to_streak(session.match)
            peak_eval_streak = max(peak_eval_streak, user.evaluation_streak)

        # 2. Прогнозы: серия угаданных по порядку окончания матчей.
        user.prediction_streak = 0
        peak_prediction_streak = 0
        decided = (
            MatchPrediction.objects.filter(user=user, match__status="finished")
            .select_related("match").order_by("match__end_time", "match__start_time")
        )
        for prediction in decided:
            if prediction.is_correct is None:
                continue
            user.prediction_streak = user.prediction_streak + 1 if prediction.is_correct else 0
            peak_prediction_streak = max(peak_prediction_streak, user.prediction_streak)

        user.save(update_fields=[
            "total_evaluations", "evaluation_streak", "last_evaluation_season_id",
            "last_evaluation_tour", "prediction_streak", "updated_at",
        ])

        # 3. XP — только за завершённые оценки, текущий множитель доверия.
        match_ids = [s.match_id for s in sessions]
        lineup_totals = dict(
            MatchLineupPlayer.objects.filter(lineup__match_id__in=match_ids)
            .values("lineup__match_id").annotate(n=Count("id")).values_list("lineup__match_id", "n")
        )
        rated = dict(
            PlayerEvaluation.objects.filter(user=user, match_id__in=match_ids)
            .values("match_id").annotate(n=Count("id")).values_list("match_id", "n")
        )
        base_xp = sum(evaluation_xp(s, lineup_totals.get(s.match_id, 0), rated.get(s.match_id, 0)) for s in sessions)
        raw = base_xp * user.xp_multiplier()
        xp, _ = UserXP.objects.select_for_update().get_or_create(user=user)
        old_xp, old_level = xp.total_xp, xp.level
        xp.total_xp = math.floor(raw + 1e-9)
        xp.xp_remainder = max(0.0, raw - xp.total_xp)
        xp.level = level_for_total_xp(xp.total_xp)
        xp.save(update_fields=["total_xp", "xp_remainder", "level", "updated_at"])

        # 4. Достижения: снимаем выведенные из данных и выдаём заново по фактам;
        #    серийные — по пику серии, а не по текущему значению. Дата получения сохраняется.
        old_badges = {
            b.badge_type: b.awarded_at
            for b in UserBadge.objects.filter(user=user).exclude(badge_type__in=PRESERVED_BADGES)
        }
        UserBadge.objects.filter(user=user).exclude(badge_type__in=PRESERVED_BADGES).delete()

    from users.services import check_and_award_badges, revalidate_status_badges

    current_streaks = (user.evaluation_streak, user.prediction_streak)
    user.evaluation_streak, user.prediction_streak = peak_eval_streak, peak_prediction_streak
    try:
        awarded = check_and_award_badges(user)
    finally:
        user.evaluation_streak, user.prediction_streak = current_streaks
    for badge in awarded:
        if badge.badge_type in old_badges:
            UserBadge.objects.filter(pk=badge.pk).update(awarded_at=old_badges[badge.badge_type])
    revalidate_status_badges(user)

    kept = {b.badge_type for b in awarded}
    summary = {
        "before": before,
        "after": {
            "total_evaluations": user.total_evaluations, "evaluation_streak": user.evaluation_streak,
            "prediction_streak": user.prediction_streak,
        },
        "xp": (old_xp, xp.total_xp), "level": (old_level, xp.level),
        "badges_removed": sorted(set(old_badges) - kept),
        "badges_added": sorted(kept - set(old_badges)),
    }
    logger.info("recompute_user_progress %s: %s", user.pk, summary)
    return summary
