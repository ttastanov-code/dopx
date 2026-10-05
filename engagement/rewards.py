# engagement/rewards.py
"""Выдача XP и достижений за механики вовлечения."""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def award_xp(user, amount: int, reason: str) -> None:
    """XP без множителя доверия: награда фиксированная и объявлена заранее."""
    from users.models import UserXP

    if amount <= 0:
        return
    xp, _ = UserXP.objects.get_or_create(user=user)
    xp.add_xp(amount)
    logger.info("engagement: +%s XP %s (%s)", amount, user.pk, reason)


def revoke_xp(user, amount: int, reason: str) -> None:
    """Забрать выданный за задание опыт (действие отменили): из уровня и из абонемента сезона."""
    from django.db.models import F, Value
    from django.db.models.functions import Greatest

    from engagement.models import SeasonPass
    from engagement.season import current_season
    from users.models import UserXP

    if amount <= 0:
        return
    xp, _ = UserXP.objects.get_or_create(user=user)
    xp.add_xp(-amount)
    season = current_season()
    if season is not None:
        SeasonPass.objects.filter(user=user, season=season).update(xp=Greatest(F("xp") - amount, Value(0)))
    logger.info("engagement: -%s XP %s (%s)", amount, user.pk, reason)


def award_badge(user, badge_type: str) -> bool:
    """Выдать достижение один раз; True — выдано сейчас (с in-app уведомлением и push)."""
    from notifications.models import Notification
    from notifications.tasks import send_push_task
    from users.badges import get_badge_definition
    from users.models import UserBadge

    if user.is_superuser:  # служебный аккаунт — без достижений
        return False
    badge, created = UserBadge.objects.get_or_create(user=user, badge_type=badge_type)
    if created:
        definition = get_badge_definition(badge_type)
        name = definition.name if definition else badge_type
        Notification.objects.create(
            user=user, notification_type="new_badge", title="🎖️ Новое достижение!",
            message=f"Вы получили достижение: {name}", action_url="/users/profile/",
        )
        send_push_task.delay([str(user.pk)], "🎖️ Новое достижение!", name, "/users/profile/", "achievement", f"badge-{user.pk}")
    return created
