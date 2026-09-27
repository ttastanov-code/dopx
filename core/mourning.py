# core/mourning.py
"""Режим траура: текущее состояние для шаблонов, баннеров и push. Настройка — в дашборде."""
from __future__ import annotations

from django.core.cache import cache
from django.utils import timezone

CACHE_KEY = "mourning:settings"
CACHE_TTL = 60

# Push, которые приходят и в траур: важная информация, не развлечение.
ESSENTIAL_PUSH_KINDS = frozenset({"match_changed", "default"})


def _settings():
    """Поля записи (без вычисления активности) — период проверяется на каждом чтении."""
    data = cache.get(CACHE_KEY)
    if data is None:
        from core.models import MourningMode

        obj = MourningMode.objects.filter(pk=1).first()
        data = {
            "is_enabled": obj.is_enabled, "message": obj.message, "starts_at": obj.starts_at,
            "ends_at": obj.ends_at, "grayscale": obj.grayscale, "hide_ads": obj.hide_ads,
            "mute_push": obj.mute_push,
        } if obj else {}
        cache.set(CACHE_KEY, data, CACHE_TTL)
    return data


def current_mourning() -> dict | None:
    """Активный режим траура (dict с опциями) или None."""
    data = _settings()
    if not data or not data["is_enabled"]:
        return None
    now = timezone.now()
    if data["starts_at"] and now < data["starts_at"]:
        return None
    if data["ends_at"] and now >= data["ends_at"]:
        return None
    return data


def forget() -> None:
    """Сбросить кэш после изменения в дашборде."""
    cache.delete(CACHE_KEY)


def push_allowed(kind: str) -> bool:
    mourning = current_mourning()
    return not (mourning and mourning["mute_push"] and kind not in ESSENTIAL_PUSH_KINDS)


def next_transition():
    """Ближайший момент смены режима (начало или конец периода) — страницы переключатся сами."""
    data = _settings()
    if not data or not data["is_enabled"]:
        return None
    now = timezone.now()
    if data["starts_at"] and data["starts_at"] > now:
        return data["starts_at"]
    if data["ends_at"] and data["ends_at"] > now:
        return data["ends_at"]
    return None
