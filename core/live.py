# core/live.py
"""Версия данных для живого обновления: любое изменение контента (оценки, агрегаты, матчи,
таблица, прогнозы) поднимает версию. Кэши контента привязаны к ней — сбрасываются сразу,
а вкладки по лёгкому пингу версии мягко обновляются (static/js/live-refresh.js).

Изменения склеиваются: версия растёт не чаще раза в GATE_SECONDS, последнее изменение
не теряется — фиксируется при следующем чтении версии.
"""
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
