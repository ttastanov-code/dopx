# adminbot/tasks.py
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
    """Утренняя сводка: цифры за вчера, очереди и состояние сервера."""
    if not tg.enabled():
        return 0
    from .handlers import cb, status_text, summary_text
    from .notify import recipients

    ids = recipients("overview")
    if not ids:
        return 0
    text = summary_text(timezone.localdate() - timedelta(days=1)) + "\n\n" + status_text().split("\n", 1)[1]
    return send_task(ids, text, [[("📋 Меню", cb("menu"))]])
