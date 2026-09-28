# engagement/streaks.py
"""Серия дней: любой визит залогиненного пользователя засчитывает день.
Каждые FREEZE_EVERY дней подряд — заморозка (до MAX_FREEZES), она закрывает пропущенный день."""
from __future__ import annotations

from datetime import timedelta

from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

MAX_FREEZES = 2
FREEZE_EVERY = 7
# День серии -> бонус XP.
MILESTONE_XP = {3: 10, 7: 25, 14: 40, 30: 80, 60: 120, 100: 200, 200: 300, 365: 500}
MILESTONE_BADGES = {7: "day_streak_7", 30: "day_streak_30", 100: "day_streak_100", 365: "day_streak_365"}


def touch(user):
    """Отметить активность сегодня (один раз в день, дёшево — через кэш)."""
    from engagement.models import DailyStreak

    today = timezone.localdate()
    key = f"streak:touched:{user.pk}:{today.isoformat()}"
    if cache.get(key):
        return None
    with transaction.atomic():
        streak, _ = DailyStreak.objects.select_for_update().get_or_create(user=user)
        advanced = apply_day(streak, today)
        streak.save()
    cache.set(key, 1, 60 * 60 * 26)
    if advanced:
        _reward_milestone(user, streak.current)
    return streak


def apply_day(streak, today) -> bool:
    """Логика дня без сохранения; True — серия выросла сегодня."""
    last = streak.last_active_date
    if last == today:
        return False
    if last == today - timedelta(days=1):
        streak.current += 1
    elif last and (today - last).days - 1 <= streak.freezes:
        missed = (today - last).days - 1
        streak.freezes -= missed
        streak.freezes_used += missed
        streak.current += 1
    else:
        streak.current = 1
    streak.last_active_date = today
    if streak.current % FREEZE_EVERY == 0 and streak.freezes < MAX_FREEZES:
        streak.freezes += 1
    streak.best = max(streak.best, streak.current)
    return True


def _reward_milestone(user, day: int) -> None:
    from engagement.rewards import award_badge, award_xp

    if day in MILESTONE_XP:
        award_xp(user, MILESTONE_XP[day], f"серия {day} дней")
    if day in MILESTONE_BADGES:
        award_badge(user, MILESTONE_BADGES[day])


def state(user) -> dict | None:
    """Для шаблонов: текущая серия с учётом того, что сегодня ещё не заходил."""
    from engagement.models import DailyStreak

    streak = DailyStreak.objects.filter(user=user).first()
    if not streak or not streak.last_active_date:
        return None
    today = timezone.localdate()
    gap = (today - streak.last_active_date).days
    alive = gap <= 1 + streak.freezes
    return {
        "current": streak.current if alive else 0,
        "best": streak.best,
        "freezes": streak.freezes,
        "done_today": gap == 0,
        "next_freeze_in": FREEZE_EVERY - (streak.current % FREEZE_EVERY) if streak.freezes < MAX_FREEZES else None,
    }
