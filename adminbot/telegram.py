# adminbot/telegram.py
"""Минимальный клиент Bot API на requests: только нужные методы, без лишних зависимостей."""
from __future__ import annotations

import logging
import re

import requests
from django.conf import settings

logger = logging.getLogger(__name__)
API = "https://api.telegram.org/bot{token}/{method}"


_TOKEN_RE = re.compile(r"bot\d+:[A-Za-z0-9_-]{20,}")


def redact(text) -> str:
    """Убирает токен бота (и другие секреты) из текста ошибки: токен есть в URL запросов к Telegram."""
    from core.redact import redact as _redact
    return _redact(text)


def _clean(e: requests.RequestException) -> requests.RequestException:
    """Та же ошибка сети, но без токена в тексте (он есть в URL)."""
    return type(e)(redact(e)) if not isinstance(e, requests.HTTPError) else requests.RequestException(redact(e))


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
    try:
        resp = requests.post(API.format(token=settings.ADMIN_BOT_TOKEN, method=method), json=params, timeout=http_timeout)
    except requests.RequestException as e:
        raise _clean(e) from None
    data = resp.json()
    if not data.get("ok"):
        raise TelegramError(data.get("error_code", resp.status_code), data.get("description", ""))
    return data["result"]


def call_files(method: str, files: dict, http_timeout: float = 60, **params):
    """Вызов с загрузкой файла (multipart): вложенные параметры — JSON-строкой."""
    import json

    data = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in params.items() if v is not None}
    try:
        resp = requests.post(API.format(token=settings.ADMIN_BOT_TOKEN, method=method), data=data, files=files,
                             timeout=http_timeout)
    except requests.RequestException as e:
        raise _clean(e) from None
    out = resp.json()
    if not out.get("ok"):
        raise TelegramError(out.get("error_code", resp.status_code), out.get("description", ""))
    return out["result"]


def _public_url(url: str) -> bool:
    # Telegram отклоняет всё сообщение, если в кнопке ссылка на localhost/IP — такие кнопки пропускаем.
    host = url.split("//", 1)[-1].split("/", 1)[0].split(":", 1)[0]
    return url.startswith("https://") and host not in ("localhost", "127.0.0.1") and not host.replace(".", "").isdigit()


# Сколько знаков помещается на кнопке телефона при 1, 2, 3 кнопках в ряд (эмодзи — за два).
ROW_FIT = {1: 34, 2: 16, 3: 10}


def text_width(text: str) -> int:
    return sum(2 if ord(c) >= 0x1F000 or 0x2600 <= ord(c) <= 0x27BF else 1 for c in text if not 0xFE00 <= ord(c) <= 0xFE0F)


def fit_rows(rows) -> list:
    """Ряд, где надпись не помещается, раскладываем по одной кнопке; слишком длинную — обрезаем в конце, а не в середине."""
    out = []
    for row in rows:
        row = [(t if text_width(t) <= ROW_FIT[1] + 4 else t[:ROW_FIT[1]].rstrip() + "…", d) for t, d in row]
        if len(row) > 1 and max(text_width(t) for t, _ in row) > ROW_FIT.get(len(row), 8):
            out += [[b] for b in row]
        else:
            out.append(row)
    return out


def keyboard(rows) -> dict:
    """[[(текст, callback_data), ...], ...] -> inline_keyboard. URL-кнопка — callback_data, начинающийся с http."""
    out = []
    for row in fit_rows(rows):
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
        logger.warning("adminbot: sendMessage failed: %s", redact(e))
        return None


def edit(chat_id: int, message_id: int, text: str, rows=None) -> None:
    params = {"chat_id": chat_id, "message_id": message_id, "text": text[:4000], "parse_mode": "HTML",
              "disable_web_page_preview": True, "reply_markup": keyboard(rows or [])}
    try:
        call("editMessageText", **params)
    except TelegramError as e:
        if "not modified" not in e.description:
            logger.warning("adminbot: editMessageText failed: %s", redact(e))
    except requests.RequestException as e:
        logger.warning("adminbot: editMessageText failed: %s", redact(e))


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
    params = {"timeout": timeout, "allowed_updates": ["message", "callback_query", "my_chat_member"]}
    if offset is not None:
        params["offset"] = offset
    return call("getUpdates", http_timeout=timeout + 10, **params)


def send_photo(chat_id, photo, caption: str = "", rows=None, filename: str = "image.png") -> dict:
    """photo — file_id/URL (str) или байты. Ошибки — TelegramError, чтобы канал записал причину."""
    params = {"chat_id": chat_id, "caption": caption[:1024], "parse_mode": "HTML"}
    if rows:
        params["reply_markup"] = keyboard(rows)
    if isinstance(photo, (bytes, bytearray)):
        return call_files("sendPhoto", {"photo": (filename, photo)}, **params)
    return call("sendPhoto", photo=photo, **params)


def send_document(chat_id, content: bytes, filename: str, caption: str = "", rows=None) -> dict | None:
    params = {"chat_id": chat_id, "caption": caption[:1024], "parse_mode": "HTML"}
    if rows:
        params["reply_markup"] = keyboard(rows)
    try:
        return call_files("sendDocument", {"document": (filename, content)}, **params)
    except (TelegramError, requests.RequestException) as e:
        logger.warning("adminbot: sendDocument failed: %s", redact(e))
        return None


def post(chat_id, text: str, rows=None) -> dict:
    """Сообщение в канал: ошибки не глотаем — их показывает очередь постов."""
    params = {"chat_id": chat_id, "text": text[:4000], "parse_mode": "HTML", "disable_web_page_preview": True}
    if rows:
        params["reply_markup"] = keyboard(rows)
    return call("sendMessage", **params)


def channel_status(chat_id) -> dict:
    """Видит ли бот канал и может ли в нём публиковать."""
    try:
        chat = call("getChat", chat_id=chat_id)
        me = call("getMe")
        member = call("getChatMember", chat_id=chat_id, user_id=me["id"])
    except (TelegramError, requests.RequestException) as e:
        return {"ok": False, "error": redact(e)[:200]}
    can_post = member.get("status") == "creator" or (member.get("status") == "administrator" and member.get("can_post_messages", True))
    return {"ok": can_post, "title": chat.get("title", ""), "username": chat.get("username", ""),
            "error": "" if can_post else "Бот не админ канала или без права публиковать"}
