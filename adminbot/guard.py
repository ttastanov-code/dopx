# adminbot/guard.py
"""Бот работает только в нашем канале: из чужих каналов и групп выходит сразу и сообщает суперпользователям."""
from __future__ import annotations

import logging

from django.conf import settings

from . import telegram as tg

logger = logging.getLogger(__name__)
JOINED = {"member", "administrator", "restricted"}


def _norm(value) -> str:
    value = str(value).strip().lower()
    return value if value.startswith(("@", "-")) or value.isdigit() else f"@{value}"


def allowed_chats() -> set[str]:
    raw = [settings.ADMIN_BOT_CHANNEL_ID] + settings.ADMIN_BOT_ALLOWED_CHATS.split(",")
    return {_norm(c) for c in raw if c and c.strip()}


def is_allowed(chat: dict) -> bool:
    ids = {_norm(chat.get("id", ""))}
    if chat.get("username"):
        ids.add(_norm("@" + chat["username"]))
    return bool(ids & allowed_chats())


def on_membership(update: dict) -> bool:
    """my_chat_member: бота добавили в чат. Чужой — выходим. True — апдейт обработан."""
    from .handlers import esc, header

    event = update.get("my_chat_member") or {}
    chat = event.get("chat", {})
    status = event.get("new_chat_member", {}).get("status")
    if chat.get("type") == "private" or status not in JOINED or is_allowed(chat):
        return True
    who = event.get("from", {})
    name = chat.get("title") or chat.get("username") or chat.get("id")
    kind = "канал" if chat.get("type") == "channel" else "чат"
    where = f"{esc(name)} · id {esc(chat.get('id'))}" + (f" · @{esc(chat['username'])}" if chat.get("username") else "")
    if chat.get("type") == "channel" and not allowed_chats():
        # Наш канал ещё не указан — не выходим, чтобы не покинуть его по ошибке.
        text = (header() + f"📣 <b>Бота добавили в канал</b>\n{where}\n\nЕсли это канал проекта, впишите в .env "
                f"<code>ADMIN_BOT_CHANNEL_ID={esc('@' + chat['username'] if chat.get('username') else chat.get('id'))}</code> "
                "и перезапустите сайт, Celery и бота. Пока канал не указан, бот в нём ничего не публикует.")
    else:
        try:
            tg.call("leaveChat", chat_id=chat.get("id"))
            left = "вышел"
        except Exception as e:
            left = f"выйти не удалось: {e}"
        logger.warning("adminbot: добавили в чужой чат %s (%s) — %s", name, chat.get("id"), left)
        text = (header() + f"🚫 <b>Бота добавили в чужой {kind}</b>\n{where}\nКто: {esc(who.get('first_name', ''))} "
                f"@{esc(who.get('username', '—'))} (id {esc(who.get('id'))})\n\nБот {esc(left)}. "
                "Публикует он только в канал из ADMIN_BOT_CHANNEL_ID.")
    try:
        from .models import BotLink

        for link in BotLink.objects.select_related("user").filter(user__is_superuser=True, user__is_active=True):
            tg.send(link.telegram_id, text)
    except Exception:
        logger.exception("adminbot: не удалось сообщить о чужом чате")
    return True
