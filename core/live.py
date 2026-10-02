# core/live.py
"""Версия данных для живого обновления: кэши контента привязаны к ней, вкладки пингуют её.
Рост не чаще раза в GATE_SECONDS, последнее изменение фиксируется при следующем чтении."""
from __future__ import annotations

from django.core.cache import cache

VERSION_KEY = "live:data_version"
PENDING_KEY = "live:data_pending"
GATE_KEY = "live:data_gate"
GATE_SECONDS = 3


def _commit_pending() -> None:
    if cache.get(PENDING_KEY) and cache.add(GATE_KEY, 1, GATE_SECONDS):
        cache.delete(PENDING_KEY)
        try:
            cache.incr(VERSION_KEY)
        except ValueError:
            cache.set(VERSION_KEY, 2, None)


def bump_data_version() -> None:
    """Отметить, что контент изменился."""
    cache.set(PENDING_KEY, 1, None)
    _commit_pending()


def data_version() -> int:
    _commit_pending()
    version = cache.get(VERSION_KEY)
    if version is None:
        cache.add(VERSION_KEY, 1, None)
        version = cache.get(VERSION_KEY) or 1
    return int(version)


def versioned(key: str) -> str:
    """Ключ кэша, который сбрасывается при изменении данных."""
    return f"{key}:v{data_version()}"


def _user_key(user_id) -> str:
    return f"live:user:{user_id}"


def bump_user_version(user_id) -> None:
    """Изменился личный прогресс (серия, XP, сезон, задания): вкладки этого пользователя обновятся."""
    key = _user_key(user_id)
    try:
        cache.incr(key)
    except ValueError:
        cache.set(key, 2, 60 * 60 * 24 * 7)


def version_for(user) -> str:
    """Общая версия данных + личная версия прогресса."""
    base = str(data_version())
    if not getattr(user, "is_authenticated", False):
        return base
    return f"{base}.{cache.get(_user_key(user.pk)) or 1}"
