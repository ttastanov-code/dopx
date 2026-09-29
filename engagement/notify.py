# engagement/notify.py
"""Уведомления механик вовлечения: in-app + push (тип — notifications.services.PUSH_PROFILES)."""
from __future__ import annotations

import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

# Кому слать опрос недели: заходил не раньше стольких дней назад.
ACTIVE_DAYS = 21


def notify(users, *, title: str, body: str, url: str, kind: str, tag: str | None = None,
           in_app: bool = True, push: bool = True) -> None:
    """Запись в колокольчик + push после коммита."""
    from notifications.models import Notification
    from notifications.tasks import send_push_task

    users = [u for u in users if u is not None]
    if not users:
        return
    if in_app:
        Notification.objects.bulk_create([
            Notification(user=u, notification_type="system", title=title, message=body, action_url=url) for u in users
        ])
    if not push:
        return
    ids = [str(u.pk) for u in users]
    transaction.on_commit(lambda: send_push_task.delay(ids, title, body, url, kind, tag))


def league_joined(league, user) -> None:
    if league.owner_id == user.pk:
        return
    notify([league.owner], title="👥 Новый участник лиги",
           body=f"{user.username} теперь в лиге «{league.name}».",
           url=f"/friends/{league.invite_code}/", kind="social", tag=f"league-{league.pk}")


def referral_rewarded(inviter, friend, xp: int) -> None:
    notify([inviter], title=f"🎁 +{xp} XP за друга",
           body=f"{friend.username} оценил первый матч. XP получили вы оба.",
           url="/invite/", kind="social", tag="referral")


def season_reward(user, level: int, title: str) -> None:
    notify([user], title=f"🎟️ Уровень {level} сезонного пропуска",
           body=f"Новая награда: {title}.", url="/season/", kind="achievement", tag="season-pass")


def active_users():
    """Пользователи, заходившие за ACTIVE_DAYS (по серии дней или входу)."""
    from django.db.models import Q

    from users.models import User

    since = timezone.now() - timedelta(days=ACTIVE_DAYS)
    return User.objects.filter(is_active=True).filter(
        Q(last_login__gte=since) | Q(daily_streak__last_active_date__gte=since.date())
    ).distinct()


def poll_published(poll) -> int:
    """Push о новом опросе недели — только push, без колокольчика."""
    users = list(active_users())
    if poll.kind == poll.KIND_EPISODE:
        title, body = f"⚖️ {poll.question}", f"{poll.context} Рассудите в один тап."
    else:
        title, body = "🥊 Дуэль тура", f"{poll.option_a} или {poll.option_b}? {poll.question}"
    notify(users, title=title, body=body[:180], url="/#polls", kind="daily_poll", tag=f"poll-{poll.kind}", in_app=False)
    return len(users)


def streaks_at_risk() -> int:
    """Вечером: серия от 3 дней, вчера заходил, сегодня — нет."""
    from engagement.models import DailyStreak

    today = timezone.localdate()
    streaks = DailyStreak.objects.filter(
        current__gte=3, last_active_date=today - timedelta(days=1), user__is_active=True,
    ).select_related("user")
    sent = 0
    for streak in streaks:
        if streak.freezes:
            body = f"Серия {streak.current} дн. Сегодня её спасёт заморозка, но их у вас всего {streak.freezes}."
        else:
            body = f"Серия {streak.current} дн. сгорит в полночь. Зайдите на сайт, чтобы её сохранить."
        notify([streak.user], title="🔥 Не потеряйте серию", body=body, url="/", kind="streak",
               tag="streak", in_app=False)
        sent += 1
    return sent
