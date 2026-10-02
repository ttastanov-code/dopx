# adminbot/notify.py
"""Уведомления сотрудникам в Telegram: только тем, у кого есть доступ к разделу и праву."""
from __future__ import annotations

from django.conf import settings
from django.db import transaction

from . import telegram as tg


def recipients(section: str, perm: str | None = None) -> list[int]:
    from .handlers import can
    from .models import BotLink

    return [l.telegram_id for l in BotLink.objects.select_related("user").filter(notify=True) if can(l.user, section, perm)]


def push(text: str, rows=None, section: str = "overview", perm: str | None = None) -> None:
    """Отправка после коммита через Celery, чтобы не держать запрос."""
    if not tg.enabled():
        return
    ids = recipients(section, perm)
    if not ids:
        return
    from .tasks import send_task

    transaction.on_commit(lambda: send_task.delay(ids, text, rows))


def expert_take(take) -> None:
    from .handlers import cb, esc, header

    match = take.match
    text = (header() + f"🎙 <b>Новое мнение эксперта</b>\n{esc(take.display_name)} · "
            f"{esc(match.home_team.name)} – {esc(match.away_team.name)}"
            + (f"\n\n<b>{esc(take.headline)}</b>" if take.headline else "") + f"\n{esc(take.text[:500])}")
    rows = [[("✅ Опубликовать", cb("take_pub", str(take.pk))), ("🗑 Удалить", cb("take_del", str(take.pk)))],
            [("Открыть", settings.SITE_URL.rstrip("/") + f"/staff/dashboard/experts/takes/{take.pk}/")]]
    push(text, rows, "experts", "engagement.view_experttake")


def antifraud_flag(flag) -> None:
    from .handlers import cb, esc, header

    target = flag.user.username if flag.user_id else str(flag.content_object or "—")
    text = (header() + f"🛡 <b>Подозрение на накрутку</b>\n{esc(flag.get_source_display())}\n"
            f"Цель: <b>{esc(target)}</b> · скор {flag.score:.2f}")
    rows = [[("⛔ Накрутка", cb("flag_ok", str(flag.pk))), ("👌 Ложный", cb("flag_no", str(flag.pk)))]]
    push(text, rows, "antifraud", "users.view_suspiciousactivityflag")


def contact(sub) -> None:
    from .handlers import cb, esc, header

    who = sub.user.username if sub.user_id else (sub.guest_email or "гость")
    text = (header() + f"✉️ <b>Новое обращение</b> · {esc(sub.get_category_display())}\n"
            f"От: {esc(who)}\n<b>{esc(sub.subject)}</b>\n{esc(sub.message[:500])}")
    push(text, [[("✅ Решено", cb("contact_done", str(sub.pk)))]], "data_trust", "notifications.view_contactsubmission")


def command_finished(chat_id: int, run) -> None:
    import re

    from .handlers import back, esc, header

    ok = run.status == run.Status.SUCCESS
    out = re.sub(r"\x1b\[[0-9;]*m", "", (run.stdout or "") + ("\n" + run.stderr if run.stderr else "")).strip()
    tail = out[-1200:] if out else "вывода нет"
    took = int((run.finished_at - run.started_at).total_seconds()) if run.finished_at and run.started_at else None
    text = (header() + ("✅" if ok else "❌") + f" <b>{esc(run.command_name)}</b> "
            + ("выполнена" if ok else "завершилась с ошибкой") + (f" за {took} с" if took is not None else "")
            + f"\n\n<pre>{esc(tail)}</pre>")
    tg.send(chat_id, text, back())
