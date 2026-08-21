# users/services.py
"""
Проверка и выдача достижений. accurate_analyst/bias_free группируют оценки
по match_id одним запросом (.values().annotate()), не N+1 в цикле по
матчам; bias_free переиспользует aggregates.services.compute_bias_score —
единый источник исторического bias-score. check_and_award_badges вызывается
только асинхронно из users/tasks.py::check_and_award_badges_task
(transaction.on_commit в evaluations/views.py) — до ~100 запросов, которые
раньше блокировали HTTP-цикл на каждом завершении оценки.

founder выдаётся отдельно при верификации email (VerifyEmailView), не
здесь — разовое событие. monthly_champion — отдельная периодическая задача
(award_monthly_champion_badge), тоже не событие на оценку. derby_hunter
считает дерби-матчи в Python по Team.rivals — join по M2M здесь менее
читаем, чем цикл по небольшому ограниченному списку пар.
"""
from __future__ import annotations

import logging

from django.db.models import Avg, F
from datetime import timedelta

from aggregates.services import compute_bias_score
from evaluations.models import ContextEvaluation, PlayerEvaluation, RefereeEvaluation
from users.models import UserBadge

logger = logging.getLogger(__name__)

ACCURATE_ANALYST_LOOKBACK = 20
ACCURATE_ANALYST_MAX_DEVIATION = 1.0
ACCURATE_ANALYST_MIN_ACCURATE_RATIO = 0.8

BIAS_FREE_LOOKBACK = 15
BIAS_FREE_MAX_SCORE = 0.15  # см. пункт 2 докстринга — доля матчей с ЭКСТРЕМАЛЬНЫМ перекосом

FORESIGHT_MIN_EVALUATIONS = 30
FORESIGHT_MIN_TRUST_SCORE = 1.6

JUDGE_OF_JUDGES_MIN_COUNT = 25
POLYGLOT_MIN_TEAMS = 8

DERBY_HUNTER_MIN_MATCHES = 5


def check_and_award_badges(user) -> list[UserBadge]:
    """
    Проверяет условия и выдаёт достижения. Возвращает список только что
    созданных объектов `UserBadge` (уже существовавшие — не возвращаются).

    ВАЖНО: вызывать эту функцию нужно ТОЛЬКО из асинхронного контекста
    (Celery-задача), не из HTTP request-response цикла — см. пункт 3
    докстринга модуля.
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

        # НОВОЕ (retention loop "Серии", 2026-08-21): прогнозы 1X2 —
        # ПАРАЛЛЕЛЬНЫЙ набор бейджей поверх user.prediction_streak (см.
        # users/models.py::User.update_prediction_stats), тот же порог 7/30/100,
        # что и у streak_7/30/100, но осознанно НЕ переиспользует их —
        # прогноз и оценка разная активность, см. докстринг у поля.
        # Не отдельный денормализованный счётчик на User (в отличие от
        # total_evaluations) — прогнозов на порядки меньше оценок за один
        # матч (одна запись MatchPrediction на пару user+match), COUNT()
        # здесь дешёвый и не требует поддерживать ещё одно поле в синхроне.
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

    except Exception as e:
        logger.error("Ошибка проверки достижений для %s: %s", user.username, e, exc_info=True)

    return awarded


def _maybe_award_accurate_analyst(user, awarded: list[UserBadge]) -> None:
    """
    Бейдж «Точный аналитик»: отклонение ≤1.0 от среднего сообщества
    (БЕЗ учёта собственной оценки пользователя) в ≥80% из последних 20
    матчей, где пользователь указывал контекст просмотра.

    2 запроса ВСЕГО вместо до 40 (см. пункт 1 докстринга модуля).
    """
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
    """
    Бейдж «Без предвзятости»: `compute_bias_score` (единая реализация из
    `aggregates.services`, см. пункт 2 докстринга модуля) по последним
    матчам поддерживаемой команды ниже `BIAS_FREE_MAX_SCORE`.
    """
    latest_context = (
        ContextEvaluation.objects.filter(user=user, supported_team__isnull=False, match__isnull=False)
        .select_related("match")
        .order_by("-created_at")
        .first()
    )
    if not latest_context or not latest_context.match_id:
        return

    bias_score = compute_bias_score(user, latest_context.match, lookback=BIAS_FREE_LOOKBACK)
    if bias_score <= BIAS_FREE_MAX_SCORE:
        b, created = UserBadge.objects.get_or_create(user=user, badge_type="bias_free")
        if created:
            awarded.append(b)


def _maybe_award_derby_hunter(user, awarded: list[UserBadge]) -> None:
    """
    Бейдж «Дерби-эксперт»: оценено ≥`DERBY_HUNTER_MIN_MATCHES` матчей между
    командами, отмеченными друг у друга как соперники (`Team.rivals`,
    проставляется вручную в админке — см. `teams/admin.py`). Список
    соперничеств — продуктовое решение, не автоматика.

    Список пар соперников в лиге заведомо маленький (десятки, не тысячи) —
    поэтому сравнение "матч — это дерби?" делается один раз в Python по
    множеству пар, а не через `.filter()` с M2M-джойном на каждый матч.
    """
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