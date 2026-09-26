# core/stats.py
"""Публичные цифры платформы — одни и те же на главной, в контактах, на панели входа и в антифроде."""
from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.db.models import Avg
from django.utils import timezone

PLATFORM_STATS_CACHE_KEY = 'platform_stats_v2'
PLATFORM_STATS_TTL = 120


def real_users():
    """Активные аккаунты; синтетические — только если их голоса считаются (COUNT_SYNTHETIC_VOTES)."""
    from core.utils import synthetic_users_q
    from users.models import User

    users = User.objects.filter(is_active=True)
    if not getattr(settings, 'COUNT_SYNTHETIC_VOTES', True):
        users = users.exclude(synthetic_users_q())
    return users


def platform_stats() -> dict:
    """Оценка = завершённая оценка матча (сессия вайзарда), а не строки по каждому игроку."""
    cached = cache.get(PLATFORM_STATS_CACHE_KEY)
    if cached is not None:
        return cached

    from aggregates.models import MatchAggregate
    from aggregates.services import published_q
    from evaluations.models import EvaluationSession
    from matches.models import Match

    now = timezone.now()
    users = real_users()
    sessions = EvaluationSession.objects.filter(status='completed', user__in=users)
    published = MatchAggregate.objects.filter(published_q(), total_votes__gt=0).aggregate(
        drama=Avg('drama_index'), entertainment=Avg('avg_entertainment'),
    )
    stats = {
        'total_matches': Match.objects.filter(status='finished').count(),
        'active_voting': Match.objects.filter(status='finished', voting_open_until__gte=now).count(),
        'total_evaluations': sessions.count(),
        'total_users': users.count(),
        'active_users': sessions.filter(completed_at__gte=now - timedelta(days=7)).values('user_id').distinct().count(),
        'avg_drama': round(published['drama']) if published['drama'] is not None else None,
        'avg_entertainment': round(published['entertainment'], 1) if published['entertainment'] is not None else None,
    }
    cache.set(PLATFORM_STATS_CACHE_KEY, stats, PLATFORM_STATS_TTL)
    return stats
