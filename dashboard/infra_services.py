# dashboard/infra_services.py
"""Операционные метрики: Redis, Celery, PostgreSQL, расписание, лог ошибок, окружение.
Каждая функция ловит свои исключения и возвращает {"ok": False, "error": ...}.
"""
from __future__ import annotations

import logging
import re

from django.conf import settings
from django.db import connection

logger = logging.getLogger(__name__)


def _redis_stats() -> dict:
    import redis

    try:
        client = redis.Redis.from_url(
            settings.CELERY_BROKER_URL, socket_connect_timeout=2, socket_timeout=2,
        )
        info = client.info()
        # Длина очереди 'celery' в Redis — сколько задач ждут.
        queue_depth = client.llen("celery")
        return {
            "ok": True,
            "used_memory_human": info.get("used_memory_human"),
            "connected_clients": info.get("connected_clients"),
            "uptime_days": round(info.get("uptime_in_seconds", 0) / 86400, 1),
            "queue_depth": queue_depth,
        }
    except Exception as e:
        logger.warning(f"infra_services._redis_stats: {e}")
        return {"ok": False, "error": str(e)}


def _celery_stats() -> dict:
    from dopx.celery import app

    try:
        # Короткий timeout — зависший воркер не должен подвесить страницу.
        inspector = app.control.inspect(timeout=1.5)
        active = inspector.active() or {}
        stats = inspector.stats() or {}
        worker_names = list(stats.keys())
        active_tasks = sum(len(tasks) for tasks in active.values())
        return {
            "ok": True,
            "workers_online": len(worker_names),
            "worker_names": worker_names,
            "active_tasks": active_tasks,
        }
    except Exception as e:
        logger.warning(f"infra_services._celery_stats: {e}")
        return {"ok": False, "error": str(e)}


def _db_stats() -> dict:
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_database_size(current_database())")
            db_size_bytes = cursor.fetchone()[0]
            cursor.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
            )
            active_connections = cursor.fetchone()[0]
        return {
            "ok": True,
            "size_mb": round(db_size_bytes / 1024 / 1024, 1),
            "active_connections": active_connections,
        }
    except Exception as e:
        logger.warning(f"infra_services._db_stats: {e}")
        return {"ok": False, "error": str(e)}


def infra_health() -> dict:
    return {
        "redis": _redis_stats(),
        "celery": _celery_stats(),
        "db": _db_stats(),
    }


# =============================================================================
# Раздел «Системный статус»
# =============================================================================


def _cache_stats(alias: str, label: str) -> dict:
    """Статус произвольного cache alias (напр. 'aggregates' — отдельная БД Redis)."""
    import redis

    location = settings.CACHES.get(alias, {}).get("LOCATION", "")
    if not location:
        return {"ok": False, "label": label, "error": "alias не настроен"}
    try:
        client = redis.Redis.from_url(location, socket_connect_timeout=2, socket_timeout=2)
        info = client.info()
        return {
            "ok": True,
            "label": label,
            "used_memory_human": info.get("used_memory_human"),
            "connected_clients": info.get("connected_clients"),
            "keys": client.dbsize(),
        }
    except Exception as e:
        logger.warning(f"infra_services._cache_stats({alias}): {e}")
        return {"ok": False, "label": label, "error": str(e)}


def beat_schedule_overview() -> list[dict]:
    """Расписание Celery Beat из settings (без «последнего запуска» — истории нет)."""
    entries = []
    for name, cfg in settings.CELERY_BEAT_SCHEDULE.items():
        schedule = cfg.get("schedule")
        entries.append({
            "name": name,
            "task": cfg.get("task", ""),
            "schedule_display": str(schedule),
        })
    return sorted(entries, key=lambda e: e["name"])


# Префикс записи verbose-форматтера: "ERROR 2026-09-23 08:32:10,123 module message".
# Строки без префикса — продолжение предыдущей записи (traceback).
_ERROR_LOG_LINE_RE = re.compile(
    r"^(?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+"
    r"(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+)\s+"
    r"(?P<module>\S+)\s+(?P<message>.*)$"
)


def recent_error_log_entries(limit: int = 15) -> dict:
    """Последние записи logs/errors.log (читаем ~300 КБ хвоста). Ошибки чтения не роняют страницу."""
    path = settings.LOGS_DIR / "errors.log"
    if not path.exists():
        return {"ok": True, "entries": [], "missing": True}
    try:
        size = path.stat().st_size
        with open(path, "r", errors="replace") as f:
            if size > 300_000:
                f.seek(size - 300_000)
                f.readline()  # первая строка может быть обрезана
            raw_lines = f.read().split("\n")
    except Exception as e:
        logger.warning(f"infra_services.recent_error_log_entries: {e}")
        return {"ok": False, "error": str(e)}

    entries = []
    current = None
    for line in raw_lines:
        if not line.strip():
            continue
        m = _ERROR_LOG_LINE_RE.match(line)
        if m:
            if current:
                entries.append(current)
            current = {
                "level": m.group("level"),
                "ts": m.group("ts"),
                "module": m.group("module"),
                "message": m.group("message"),
                "extra": [],
            }
        elif current:
            current["extra"].append(line)
    if current:
        entries.append(current)

    entries.reverse()  # новые сверху
    for e in entries:
        # На дашборде — только первые строки traceback.
        e["extra"] = e["extra"][:6]
    return {"ok": True, "entries": entries[:limit], "missing": False, "truncated_scan": size > 300_000}


def environment_info() -> dict:
    """Версии и окружение; DEBUG=True подсвечивается."""
    import platform

    import django as django_module

    return {
        "django_version": django_module.get_version(),
        "python_version": platform.python_version(),
        "debug": settings.DEBUG,
        "time_zone": settings.TIME_ZONE,
        "db_engine": settings.DATABASES["default"]["ENGINE"].rsplit(".", 1)[-1],
    }


def system_status_overview() -> dict:
    return {
        "infra": infra_health(),
        "aggregates_cache": _cache_stats("aggregates", "Кэш агрегатов"),
        "beat_schedule": beat_schedule_overview(),
        "error_log": recent_error_log_entries(),
        "env": environment_info(),
    }
