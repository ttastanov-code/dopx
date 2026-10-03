# fanbot/auth.py
"""Проверка подписи Telegram: виджет входа на сайте и initData Mini App. Без валидной подписи — None."""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

from django.conf import settings

# Данные старше суток не принимаем — защита от повторного использования.
MAX_AGE = 24 * 3600
WIDGET_FIELDS = ("id", "first_name", "last_name", "username", "photo_url", "auth_date")


def _fresh(auth_date) -> bool:
    try:
        return 0 <= time.time() - int(auth_date) <= MAX_AGE
    except (TypeError, ValueError):
        return False


def verify_login_widget(data: dict) -> dict | None:
    """Виджет входа: hash = HMAC-SHA256(SHA256(token), «ключ=значение» по алфавиту через \\n)."""
    token = settings.FAN_BOT_TOKEN
    received = data.get("hash", "")
    if not token or not received:
        return None
    # Подписаны только поля Telegram; служебные параметры (next) не участвуют.
    fields = {k: v for k, v in data.items() if k in WIDGET_FIELDS and v not in (None, "")}
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hashlib.sha256(token.encode()).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received) or not _fresh(fields.get("auth_date")):
        return None
    # Виджет запрашивает право писать (data-request-access="write"); отказ проявится ошибкой 403 при отправке.
    return {"id": int(fields["id"]), "username": fields.get("username", ""), "first_name": fields.get("first_name", ""),
            "last_name": fields.get("last_name", ""), "photo_url": fields.get("photo_url", ""), "allows_write": True}


def verify_webapp(init_data: str) -> dict | None:
    """Mini App: secret = HMAC-SHA256(«WebAppData», token), hash — от отсортированных полей initData."""
    token = settings.FAN_BOT_TOKEN
    if not token or not init_data:
        return None
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received = pairs.pop("hash", "")
    if not received:
        return None
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()

    def signed(fields: dict) -> bool:
        check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
        return hmac.compare_digest(hmac.new(secret, check.encode(), hashlib.sha256).hexdigest(), received)

    # Поле signature (Ed25519) входит в строку для hash; без него — старые клиенты Telegram.
    without_sig = {k: v for k, v in pairs.items() if k != "signature"}
    if not (signed(pairs) or signed(without_sig)) or not _fresh(pairs.get("auth_date")):
        return None
    try:
        user = json.loads(pairs.get("user", "{}"))
    except ValueError:
        return None
    if not user.get("id"):
        return None
    return {"id": int(user["id"]), "username": user.get("username", ""), "first_name": user.get("first_name", ""),
            "last_name": user.get("last_name", ""), "photo_url": user.get("photo_url", ""),
            "start_param": pairs.get("start_param", ""), "allows_write": bool(user.get("allows_write_to_pm"))}
