# engagement/tasks.py
from __future__ import annotations

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task
def generate_weekly_social_content():
    """Раз в неделю готовит картинки для соцсетей и шлёт staff уведомление."""
    from engagement.social import weekly_content
    from notifications.models import Notification
    from users.models import User

    data = weekly_content(force=True)
    if not data or not data["posts"]:
        logger.info("social content: нет данных тура")
        return 0
    staff = User.objects.filter(is_staff=True, is_active=True)
    Notification.objects.bulk_create([
        Notification(
            user=u, notification_type="system", title="Контент для соцсетей готов",
            message=f"{data['title']}: {len(data['posts'])} картинки с подписями. Дашборд → Соцсети.",
            action_url="/staff/dashboard/social/",
        ) for u in staff
    ])
    return len(data["posts"])


# День недели (Mon=0) -> тип опроса.
POLL_SCHEDULE = {1: "episode", 2: "duel"}


@shared_task
def publish_weekly_polls(kind: str | None = None, force: bool = False):
    """Вт — «Спорный момент», ср — «Дуэль тура». С kind/force — вне расписания."""
    from django.utils import timezone

    from engagement.notify import poll_published
    from engagement.polls import create_poll

    kind = kind or POLL_SCHEDULE.get(timezone.localdate().weekday())
    if not kind:
        return None
    poll = create_poll(kind, force=force)
    if poll is None:
        logger.info("weekly poll %s: нечего публиковать или уже есть", kind)
        return None
    poll_published(poll)
    return str(poll.pk)


@shared_task
def notify_streaks_at_risk():
    from engagement.notify import streaks_at_risk

    return streaks_at_risk()
