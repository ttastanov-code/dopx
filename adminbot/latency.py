# adminbot/latency.py
"""Время ответа сайта: счётчики по корзинам за минуту в кэше, из них — p95 для алерта."""
from __future__ import annotations

import time

from django.core.cache import cache

BUCKETS = (0.1, 0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 10.0, float("inf"))
TTL = 20 * 60
SKIP_PREFIXES = ("/static/", "/media/", "/healthz", "/bot/relay/", "/api/live-version/", "/favicon")


def _key(minute: int, i: int) -> str:
    return f"lat:{minute}:{i}"


def record(seconds: float, now: float | None = None) -> None:
    minute = int((now or time.time()) // 60)
    i = next(n for n, b in enumerate(BUCKETS) if seconds <= b)
    key = _key(minute, i)
    try:
        if not cache.add(key, 1, TTL):
            cache.incr(key)
    except Exception:
        pass  # замер не должен ломать запрос


def p95(minutes: int = 5, now: float | None = None) -> tuple[float | None, int]:
    """(p95 в секундах — верхняя граница корзины, число запросов) за последние минуты."""
    current = int((now or time.time()) // 60)
    counts = [0] * len(BUCKETS)
    keys = [_key(m, i) for m in range(current - minutes + 1, current + 1) for i in range(len(BUCKETS))]
    for key, value in cache.get_many(keys).items():
        counts[int(key.rsplit(":", 1)[1])] += int(value)
    total = sum(counts)
    if not total:
        return None, 0
    need, seen = total * 0.95, 0
    for i, c in enumerate(counts):
        seen += c
        if seen >= need:
            return BUCKETS[i], total
    return BUCKETS[-1], total


class LatencyMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path.startswith(SKIP_PREFIXES):
            return self.get_response(request)
        started = time.monotonic()
        response = self.get_response(request)
        if not getattr(response, "streaming", False):
            record(time.monotonic() - started)
        return response
