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
    """Запланированные посты канала, эскалация инцидентов, уборка старых пробных черновиков."""
    if not tg.enabled():
        return {}
    from . import channel, incidents

    return {"published": channel.publish_due(), "escalated": incidents.escalate_due(),
            "samples_dropped": channel.drop_old_samples()}


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


@shared_task(ignore_result=True)
def channel_rubric() -> int:
    """12:00: вторник — спорный момент тура, среда — цифра недели."""
    if not tg.enabled():
        return 0
    from .channel import weekly_rubric

    return len(weekly_rubric())


@shared_task(ignore_result=True)
def make_samples(chat_id: int | None = None) -> int:
    """Пробные посты всех форматов (Claude пишет тексты — это может занять минуту-две)."""
    from .channel import sample_posts

    posts = sample_posts()
    if chat_id:
        from .handlers import cb, header

        ai = sum(p.by_ai for p in posts)
        tg.send(chat_id, header() + f"🧪 Готово пробных черновиков: {len(posts)}"
                + (f", тексты Claude: {ai}" if ai else ", тексты по шаблонам") + ". Откройте «Черновики».",
                [[("📝 Черновики", cb("chan_list", "draft"))]])
    return len(posts)



@shared_task(ignore_result=True)
def watch_admin_bot():
    """Сторож сторожа: алерты шлёт бот команды, поэтому его самого проверяет Celery и пишет в Telegram напрямую."""
    from django.core.cache import cache

    from core import heartbeat

    from . import alerts
    from . import telegram as tg
    from .notify import recipients

    row = next(r for r in heartbeat.overview() if r["name"] == "admin_bot")
    if row["status"] != "down" or not alerts.enabled() or not cache.add("adminbot:watch:sent", 1, 30 * 60):
        return
    text = ("🔴 <b>Бот команды не отвечает</b>\n"
            + (f"Последний пульс {row['age'] // 60} мин назад." if row["age"] else "Пульса не было.")
            + "\nDocker перезапускает контейнер сам; если не поднимется — Дашборд → Системный статус.")
    for chat_id in recipients("system_status", topic="incidents"):
        try:
            tg.send(chat_id, text)
        except Exception:
            pass
