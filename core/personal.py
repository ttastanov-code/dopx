# core/personal.py
"""Личная сводка пользователя: что ему сейчас стоит сделать (главная, нижняя панель)."""
from __future__ import annotations

from datetime import timedelta

from django.core.cache import cache
from django.db.models import Exists, OuterRef
from django.utils import timezone

from evaluations.models import EvaluationSession
from matches.models import Match
from predictions.models import MatchPrediction

# Кэш счётчика для нижней панели (запрос на каждую страницу).
PENDING_COUNT_CACHE_SECONDS = 60


def votable_matches_for(user):
    """Матчи с открытым голосованием, которые пользователь ещё не оценил (ближайший дедлайн первым)."""
    now = timezone.now()
    completed = EvaluationSession.objects.filter(user=user, match=OuterRef('pk'), status='completed')
    return (
        Match.objects.filter(status='finished', voting_open_until__gte=now)
        .exclude(Exists(completed))
        .select_related('home_team', 'away_team')
        .order_by('voting_open_until')
    )


def pending_evaluations_count(user) -> int:
    """Сколько матчей ждут оценки пользователя (кэш на минуту)."""
    if not getattr(user, 'is_authenticated', False):
        return 0
    key = f'personal:pending_evals:{user.pk}'
    count = cache.get(key)
    if count is None:
        count = votable_matches_for(user).count()
        cache.set(key, count, PENDING_COUNT_CACHE_SECONDS)
    return count


def forget_pending_count(user_id) -> None:
    """Сбросить кэш счётчика (после завершения оценки)."""
    cache.delete(f'personal:pending_evals:{user_id}')


def personal_summary(user) -> dict:
    """Всё для панели «Ваш день» на главной."""
    now = timezone.now()
    votable = votable_matches_for(user)
    next_match = votable.first()

    in_progress = (
        EvaluationSession.objects.filter(
            user=user, status__in=['started', 'in_progress'],
            match__status='finished', match__voting_open_until__gte=now,
        )
        .select_related('match__home_team', 'match__away_team')
        .order_by('-updated_at')
        .first()
    )

    predicted = MatchPrediction.objects.filter(user=user, match=OuterRef('pk'))
    predictable = (
        Match.objects.filter(
            status='scheduled', start_time__gt=now,
            start_time__lte=now + timedelta(days=Match.PREDICTION_WINDOW_DAYS),
        )
        .exclude(Exists(predicted))
        .select_related('home_team', 'away_team')
        .order_by('start_time')
    )

    xp = getattr(user, 'xp', None)
    return {
        'pending_count': votable.count(),
        'next_match': next_match,
        'next_deadline': next_match.voting_open_until if next_match else None,
        'in_progress': in_progress,
        # Продолжить с первого непройденного шага.
        'resume_url': (in_progress.next_step_url(in_progress.match_id) if in_progress else None),
        'predictions_count': predictable.count(),
        'next_prediction': predictable.first(),
        'level': xp.level if xp else 1,
        'xp_progress': xp.progress_percent if xp else 0,
        'xp_to_next': max(0, xp.xp_for_next_level - xp.total_xp) if xp else 0,
        'streak': user.evaluation_streak,
        'trust_level': user.get_trust_level()[1],
    }
