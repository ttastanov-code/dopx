# engagement/season.py
"""Сезонный пропуск: 30 уровней по XP_PER_LEVEL, каждый сезон — с нуля.
Награды — рамки аватара, достижения и «золотой ник» (навсегда)."""
from __future__ import annotations

import logging

from django.core.cache import cache
from django.db.models import F

logger = logging.getLogger(__name__)

LEVELS = 30
XP_PER_LEVEL = 100
# Уровень -> награда: kind — frame/badge/golden_name.
REWARDS = {
    5: {"kind": "frame", "value": "bronze", "title": "Бронзовая рамка аватара", "icon": "ti-circle"},
    10: {"kind": "badge", "value": "season_pass_10", "title": "Достижение «Болельщик сезона»", "icon": "ti-award"},
    15: {"kind": "frame", "value": "silver", "title": "Серебряная рамка аватара", "icon": "ti-circle-half-2"},
    20: {"kind": "badge", "value": "season_pass_20", "title": "Достижение «Опора трибун»", "icon": "ti-medal"},
    25: {"kind": "frame", "value": "gold", "title": "Золотая рамка аватара", "icon": "ti-circle-dot"},
    30: {"kind": "golden_name", "value": "season_pass_30", "title": "Золотой ник + легендарное достижение", "icon": "ti-crown"},
}
FRAME_ORDER = {"bronze": 1, "silver": 2, "gold": 3}


def level_for(xp: int) -> int:
    return min(LEVELS, xp // XP_PER_LEVEL + 1)


def current_season():
    from seasons.models import Season

    return Season.get_primary_active()


def add_season_xp(user_id, amount: int) -> None:
    """XP в пропуск активного сезона + выдача наград за новые уровни."""
    from engagement.models import SeasonPass
    from users.models import User

    season = current_season()
    if season is None or amount <= 0:
        return
    season_pass, _ = SeasonPass.objects.get_or_create(user_id=user_id, season=season)
    SeasonPass.objects.filter(pk=season_pass.pk).update(xp=F("xp") + amount)
    season_pass.refresh_from_db()
    level = level_for(season_pass.xp)
    new = [lvl for lvl in REWARDS if lvl <= level and lvl not in season_pass.claimed_levels]
    if not new:
        return
    user = User.objects.get(pk=user_id)
    from engagement.rewards import award_badge

    from engagement.notify import season_reward

    for lvl in sorted(new):
        reward = REWARDS[lvl]
        if reward["kind"] in ("badge", "golden_name"):
            award_badge(user, reward["value"])
        if reward["kind"] in ("frame", "golden_name"):
            season_reward(user, lvl, reward["title"])
    season_pass.claimed_levels = sorted(set(season_pass.claimed_levels) | set(new))
    season_pass.save(update_fields=["claimed_levels", "updated_at"])
    cache.delete(f"cosmetics:{user_id}")


def overview(user) -> dict | None:
    """Прогресс пропуска для страниц и панели."""
    from engagement.models import SeasonPass

    season = current_season()
    if season is None:
        return None
    season_pass = SeasonPass.objects.filter(user=user, season=season).first() if user.is_authenticated else None
    xp = season_pass.xp if season_pass else 0
    level = level_for(xp)
    into = xp - (level - 1) * XP_PER_LEVEL
    return {
        "season": season, "xp": xp, "level": level, "max_level": LEVELS,
        "is_max": level >= LEVELS,
        "level_progress": 100 if level >= LEVELS else round(into * 100 / XP_PER_LEVEL),
        "to_next": 0 if level >= LEVELS else XP_PER_LEVEL - into,
        "claimed": set(season_pass.claimed_levels) if season_pass else set(),
    }


def cosmetics(user_id) -> dict:
    """Лучшие полученные награды за все сезоны: рамка аватара и золотой ник."""
    from engagement.models import SeasonPass

    key = f"cosmetics:{user_id}"
    cached = cache.get(key)
    if cached is not None:
        return cached
    claimed = set()
    for levels in SeasonPass.objects.filter(user_id=user_id).values_list("claimed_levels", flat=True):
        claimed |= set(levels or [])
    frames = [REWARDS[lvl]["value"] for lvl in claimed if REWARDS.get(lvl, {}).get("kind") == "frame"]
    result = {
        "frame": max(frames, key=FRAME_ORDER.get) if frames else "",
        "golden_name": any(REWARDS.get(lvl, {}).get("kind") == "golden_name" for lvl in claimed),
    }
    cache.set(key, result, 600)
    return result
