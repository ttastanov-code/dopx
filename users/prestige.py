# users/prestige.py
"""Статус болельщика по достижениям: витрина (до трёх медалей), звание у ника и рамка аватара.
Секретные достижения не показываются: их условия скрыты от остальных."""
from __future__ import annotations

from django.core.cache import cache

from users.badges import BADGE_CATALOG, RARITY_ORDER, badge_icon

SHOWCASE_MAX = 3
# Звание у ника и рамка — только с золота: иначе статус есть у всех и ничего не значит.
TITLE_RARITIES = ("gold", "platinum", "legendary")


def _key(user_id) -> str:
    return f"prestige:{user_id}"


def invalidate(user_id) -> None:
    cache.delete(_key(user_id))


def _item(code: str) -> dict:
    d = BADGE_CATALOG[code]
    return {"code": code, "name": d.name, "rarity": d.rarity, "icon": badge_icon(code)}


def prestige(user_id) -> dict:
    """{'showcase': [...], 'title': {...}|None, 'frame': 'gold'|'platinum'|'legendary'|''}."""
    cached = cache.get(_key(user_id))
    if cached is not None:
        return cached
    from users.models import User, UserBadge

    rows = list(UserBadge.objects.filter(user_id=user_id, is_stale=False).values_list("badge_type", "awarded_at"))
    earned = [(c, at) for c, at in rows if c in BADGE_CATALOG and not BADGE_CATALOG[c].is_secret]
    earned.sort(key=lambda r: (RARITY_ORDER.get(BADGE_CATALOG[r[0]].rarity, 0), r[1]), reverse=True)
    codes = [c for c, _ in earned]
    pinned = [c for c in (User.objects.filter(pk=user_id).values_list("showcase_badges", flat=True).first() or []) if c in codes]
    showcase = (pinned or codes)[:SHOWCASE_MAX]
    best = codes[0] if codes else None
    top = showcase[0] if pinned else best
    title = _item(top) if top and BADGE_CATALOG[top].rarity in TITLE_RARITIES else None
    frame = BADGE_CATALOG[best].rarity if best and BADGE_CATALOG[best].rarity in TITLE_RARITIES else ""
    result = {"showcase": [_item(c) for c in showcase], "title": title, "frame": frame, "pinned": pinned}
    cache.set(_key(user_id), result, 600)
    return result
