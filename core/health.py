# core/health.py
"""/healthz/ для деплоя и мониторинга: база, Redis, пульс Celery, версия кода.
503 — если недоступна база или Redis (сайт не работает). Отставший пульс Celery — «degraded», но 200:
сайт отвечает, а фоновые задачи надо проверить."""
from __future__ import annotations

import time

from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.http import JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.cache import never_cache

# Пульс ставится раз в минуту; старше этого — фоновые задачи стоят.
HEARTBEAT_STALE_SECONDS = 180


def _check_db() -> bool:
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        return True
    except Exception:
        return False


def _check_cache() -> bool:
    try:
        cache.set("healthz:ping", "1", 10)
        return cache.get("healthz:ping") == "1"
    except Exception:
        return False


def celery_heartbeat_age() -> int | None:
    """Секунд с последнего пульса Celery; None — пульса ещё не было."""
    from core.tasks import HEARTBEAT_CACHE_KEY

    try:
        raw = cache.get(HEARTBEAT_CACHE_KEY)
    except Exception:
        return None
    moment = parse_datetime(raw) if raw else None
    return int((timezone.now() - moment).total_seconds()) if moment else None


@never_cache
def healthz(request):
    started = time.monotonic()
    db_ok, cache_ok = _check_db(), _check_cache()
    age = celery_heartbeat_age()
    celery_ok = age is not None and age < HEARTBEAT_STALE_SECONDS
    if not (db_ok and cache_ok):
        status = "fail"
    else:
        status = "ok" if celery_ok else "degraded"
    return JsonResponse({
        "status": status,
        "version": settings.APP_VERSION,
        "commit": settings.APP_COMMIT,
        "db": db_ok,
        "cache": cache_ok,
        "celery_heartbeat_age": age,
        "ms": round((time.monotonic() - started) * 1000),
    }, status=503 if status == "fail" else 200)
