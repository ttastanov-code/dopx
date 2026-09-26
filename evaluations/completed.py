# evaluations/completed.py
"""Незавершённая оценка никуда не идёт: ни в рейтинги, ни в профиль, ни в награды и XP."""
from __future__ import annotations

from django.db.models import Exists, OuterRef

from evaluations.models import EvaluationSession


def completed_only(queryset, user_field: str = 'user_id', match_field: str = 'match_id'):
    """Строки оценок (Context/Player/Team/…), у которых есть завершённая сессия того же пользователя и матча."""
    done = EvaluationSession.objects.filter(
        user_id=OuterRef(user_field), match_id=OuterRef(match_field), status='completed',
    )
    return queryset.filter(Exists(done))


def completed_sessions(user):
    return EvaluationSession.objects.filter(user=user, status='completed')
