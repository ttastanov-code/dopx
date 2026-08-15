# dashboard/infra_services.py
"""
Операционные метрики платформы (продуктовый апгрейд, "куча полезных
метрик... операционные" — техническое здоровье системы, а не продуктовые
цифры типа DAU/выручки). Три источника: Redis (брокер celery + кэш),
Celery (воркеры/очередь), PostgreSQL (размер БД, соединения).

ВАЖНО: каждая функция ловит СВОИ исключения и возвращает {"ok": False,
"error": ...} вместо падения — эта секция дашборда сама по себе диагностика
инфраструктуры, она не должна ломаться ИМЕННО тогда, когда инфраструктура
уже нездорова (Redis недоступен → страница здоровья не должна превращаться
в 500-ю ошибку, а обязана явно показать "Redis недоступен").
"""
from __future__ import annotations

import logging

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
        # 'celery' — имя очереди по умолчанию (CELERY_TASK_DEFAULT_QUEUE не
        # переопределён нигде в settings.py, значит используется дефолт).
        # Это LIST в Redis — необработанные таски лежат в нём, пока воркер
        # их не заберёт; глубина = сколько задач ЖДУТ, а не выполняются.
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
        # Короткий timeout намеренно: control.inspect() рассылает broadcast
        # всем воркерам и ЖДЁТ ответа — без явного лимита один зависший
        # воркер способен подвесить загрузку всей страницы data-health.
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
