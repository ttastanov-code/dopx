# adminbot/telegram.py
"""Минимальный клиент Bot API на requests: только нужные методы, без лишних зависимостей."""
from __future__ import annotations

import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)
API = "https://api.telegram.org/bot{token}/{method}"


class TelegramError(Exception):
    def __init__(self, code: int, description: str):
        super().__init__(f"{code}: {description}")
        self.code = code
        self.description = description


def enabled() -> bool:
    return bool(settings.ADMIN_BOT_ENABLED and settings.ADMIN_BOT_TOKEN)


def call(method: str, http_timeout: float = 15, **params):
    """Вызов метода; ошибка Telegram — TelegramError (409 — бота уже слушает другой процесс).
    http_timeout — таймаут запроса; у getUpdates есть свой параметр timeout (долгий опрос)."""
    resp = requests.post(API.format(token=settings.ADMIN_BOT_TOKEN, method=method), json=params, timeout=http_timeout)
    data = resp.json()
    if not data.get("ok"):
        raise TelegramError(data.get("error_code", resp.status_code), data.get("description", ""))
    return data["result"]


def _public_url(url: str) -> bool:
    # Telegram отклоняет всё сообщение, если в кнопке ссылка на localhost/IP — такие кнопки пропускаем.
    host = url.split("//", 1)[-1].split("/", 1)[0].split(":", 1)[0]
    return url.startswith("https://") and host not in ("localhost", "127.0.0.1") and not host.replace(".", "").isdigit()


def keyboard(rows) -> dict:
    """[[(текст, callback_data), ...], ...] -> inline_keyboard. URL-кнопка — callback_data, начинающийся с http."""
    out = []
    for row in rows:
        buttons = []
        for text, data in row:
            if data.startswith("http"):
                if _public_url(data):
                    buttons.append({"text": text, "url": data})
            else:
                buttons.append({"text": text, "callback_data": data[:64]})
        if buttons:
            out.append(buttons)
    return {"inline_keyboard": out}


def send(chat_id: int, text: str, rows=None, silent: bool = False) -> dict | None:
    params = {"chat_id": chat_id, "text": text[:4000], "parse_mode": "HTML", "disable_web_page_preview": True,
              "disable_notification": silent}
    if rows:
        params["reply_markup"] = keyboard(rows)
    try:
        return call("sendMessage", **params)
    except (TelegramError, requests.RequestException) as e:
        logger.warning("adminbot: sendMessage failed: %s", e)
        return None


def edit(chat_id: int, message_id: int, text: str, rows=None) -> None:
    params = {"chat_id": chat_id, "message_id": message_id, "text": text[:4000], "parse_mode": "HTML",
              "disable_web_page_preview": True, "reply_markup": keyboard(rows or [])}
    try:
        call("editMessageText", **params)
    except TelegramError as e:
        if "not modified" not in e.description:
            logger.warning("adminbot: editMessageText failed: %s", e)
    except requests.RequestException as e:
        logger.warning("adminbot: editMessageText failed: %s", e)


def answer(callback_id: str, text: str = "", alert: bool = False) -> None:
    try:
        call("answerCallbackQuery", callback_query_id=callback_id, text=text[:190], show_alert=alert)
    except (TelegramError, requests.RequestException):
        pass


def delete(chat_id: int, message_id: int) -> None:
    try:
        call("deleteMessage", chat_id=chat_id, message_id=message_id)
    except (TelegramError, requests.RequestException):
        pass


def get_updates(offset: int | None, timeout: int = 25) -> list[dict]:
    params = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
    if offset is not None:
        params["offset"] = offset
    return call("getUpdates", http_timeout=timeout + 10, **params)
