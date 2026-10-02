from datetime import timedelta

from celery import shared_task
from django.utils import timezone

from . import telegram as tg

# Порог скора, с которого флаг антифрода приходит в Telegram.
FLAG_NOTIFY_SCORE = 0.6


@shared_task(ignore_result=True)
def send_task(chat_ids: list[int], text: str, rows=None) -> int:
    sent = 0
    for chat_id in chat_ids:
        if tg.send(chat_id, text, rows):
            sent += 1
    return sent


@shared_task(ignore_result=True)
def daily_digest() -> int:
    """Утренняя сводка: цифры за вчера с трендами, очереди и состояние сервера."""
    if not tg.enabled():
        return 0
    from .handlers import cb, status_text
    from .notify import recipients
    from .reports import digest_text

    ids = recipients("overview", topic="digest")
    if not ids:
        return 0
    text = digest_text(timezone.localdate() - timedelta(days=1)) + "\n\n" + status_text().split("\n", 1)[1]
    return send_task(ids, text, [[("📋 Меню", cb("menu")), ("⚽ Матчдень", cb("md"))]])


@shared_task(ignore_result=True)
def matchday_tick() -> dict:
    if not tg.enabled():
        return {}
    from .matchday import tick

    return tick()


@shared_task(ignore_result=True)
def minute_tick() -> dict:
    """Запланированные посты канала и эскалация инцидентов."""
    if not tg.enabled():
        return {}
    from . import channel, incidents

    return {"published": channel.publish_due(), "escalated": incidents.escalate_due()}


@shared_task(ignore_result=True)
def experts_reminder() -> int:
    """Среда: сколько мнений на ближайший тур и кто из обычных экспертов ещё не написал."""
    if not tg.enabled():
        return 0
    from .experts import coverage, coverage_text
    from .handlers import cb
    from .notify import recipients

    data = coverage()
    if not data or not data["missing"]:
        return 0
    ids = recipients("experts", "engagement.add_expertinvite", topic="queues")
    rows = [[("🔗 Ссылки тем, кто не написал", cb("inv_missing"))], [("🎙 Эксперты", cb("exp"))]]
    return send_task(ids, coverage_text(data) + "\n\nНапомнить?", rows)


@shared_task(ignore_result=True)
def weekly_staff_report() -> int:
    """Понедельник: что делала команда, рискованные действия, кто давно не заходил — суперпользователям."""
    if not tg.enabled():
        return 0
    from .models import BotLink
    from .reports import weekly_staff_text

    ids = [l.telegram_id for l in BotLink.objects.select_related("user").filter(user__is_superuser=True, user__is_active=True)
           if l.wants("weekly")]
    if not ids:
        return 0
    text, rows = weekly_staff_text()
    return send_task(ids, text, rows or None)
