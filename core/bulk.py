# core/bulk.py
"""Тихий режим для массовых скриптов (наполнение и очистка истории): они пересчитывают всё сами,
поэтому сигналы не ставят фоновые задачи и не запускают рассылки — иначе очередь Celery забивается на час."""
from __future__ import annotations

import threading
from contextlib import contextmanager

_state = threading.local()


@contextmanager
def quiet():
    previous = getattr(_state, "on", False)
    _state.on = True
    try:
        yield
    finally:
        _state.on = previous


def is_quiet() -> bool:
    return getattr(_state, "on", False)
