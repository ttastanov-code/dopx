# core/safe_mode.py
"""Безопасный режим ноутбука: DEV_SAFE_MODE=True выключает всё, что тратит лимиты или уходит людям.
Включить отдельное обратно — DEV_SAFE_ALLOW=ai,channel,... На проде режим не действует."""
from __future__ import annotations

from django.conf import settings

KINDS = {
    "sportmonks": "Синхронизация Sportmonks",
    "ai": "ИИ: Claude и Gemini",
    "channel": "Публикации в Telegram-канал",
    "email": "Настоящая почта (иначе письма — в консоль)",
    "push": "Push-уведомления пользователям",
}


def active() -> bool:
    return bool(getattr(settings, "DEV_SAFE_MODE", False))


def allowed(kind: str) -> bool:
    return not active() or kind in getattr(settings, "DEV_SAFE_ALLOW", set())


def overview() -> list[dict]:
    return [{"key": k, "label": label, "allowed": allowed(k)} for k, label in KINDS.items()]
