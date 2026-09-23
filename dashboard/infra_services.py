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


# =============================================================================
# 2026-09-23, раздел «Системный статус» (dashboard/views.py::system_status) —
# отдельная страница-сводка "жива ли платформа технически", а не только
# кусок инфры внутри «Здоровье данных» (там infra_health() уже был, но
# посреди данных синка, без расписания задач/логов ошибок/версий). Новые
# функции ниже, infra_health() выше переиспользуется как есть.
# =============================================================================


def _cache_stats(alias: str, label: str) -> dict:
    """Redis-статистика произвольного cache alias из settings.CACHES —
    ОТДЕЛЬНО от _redis_stats() выше (тот всегда смотрит на CELERY_BROKER_
    URL, т.е. на брокер задач). 'aggregates' — физически другая БД Redis
    (redis://.../2, см. докстринг про баг с совпавшими дефолтами в
    dopx/settings.py::CACHES) — может быть недоступна независимо от
    брокера/дефолтного кэша, поэтому статус нужен отдельным блоком."""
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
    """Человекочитаемый список периодических задач из settings.CELERY_BEAT_
    SCHEDULE — единственный источник расписания в проекте (django_celery_
    beat не установлен, планировщик встроенный файловый, задачи целиком
    статичны в settings.py, правятся только деплоем). Показываем "что
    вообще должно выполняться и когда" без похода в код.

    Намеренно БЕЗ "последний запуск": персистентная история есть только у
    Sportmonks-синка (ParserSyncRun, см. «Здоровье данных») — у остальных
    задач (пересчёт агрегатов, антифрод, дайджесты) нет своей модели-лога,
    только строки в logs/celery.log. Добавлять фейковое "последний раз: ?"
    хуже, чем не добавлять ничего — тут только факт расписания."""
    entries = []
    for name, cfg in settings.CELERY_BEAT_SCHEDULE.items():
        schedule = cfg.get("schedule")
        entries.append({
            "name": name,
            "task": cfg.get("task", ""),
            "schedule_display": str(schedule),
        })
    return sorted(entries, key=lambda e: e["name"])


# Формат из dopx/settings.py::LOGGING → 'verbose' → '{levelname} {asctime}
# {module} {message}', напр. "ERROR 2026-09-23 08:32:10,123 tasks Что-то
# упало". Любая строка БЕЗ этого префикса (traceback от exc_info=True)
# считается продолжением предыдущей записи, см. recent_error_log_entries().
_ERROR_LOG_LINE_RE = re.compile(
    r"^(?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+"
    r"(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+)\s+"
    r"(?P<module>\S+)\s+(?P<message>.*)$"
)


def recent_error_log_entries(limit: int = 15) -> dict:
    """Хвост logs/errors.log (RotatingFileHandler, только уровня ERROR —
    см. LOGGING в dopx/settings.py) — "что реально падало на сервере за
    последнее время" прямо на дашборде, без похода за файлом руками.

    Читаем ТОЛЬКО последние ~300КБ файла (не весь — ротация на 10МБ,
    целиком парсить незачем), группируем построчно в записи: новая
    запись начинается со строки в формате verbose-форматтера, все
    последующие строки без этого префикса (Python-traceback) — хвост
    предыдущей записи. Ловит и логгирует СВОЮ ошибку, а не падает —
    страница статуса не должна класть саму себя, если файл лога вдруг
    недоступен (см. докстринг модуля)."""
    path = settings.LOGS_DIR / "errors.log"
    if not path.exists():
        return {"ok": True, "entries": [], "missing": True}
    try:
        size = path.stat().st_size
        with open(path, "r", errors="replace") as f:
            if size > 300_000:
                f.seek(size - 300_000)
                f.readline()  # отбрасываем обрезанную первую строку
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
        # Полный traceback всё равно живёт в самом файле на сервере —
        # на дашборде достаточно первых нескольких строк, чтобы понять,
        # что это было, без простыни на весь экран.
        e["extra"] = e["extra"][:6]
    return {"ok": True, "entries": entries[:limit], "missing": False, "truncated_scan": size > 300_000}


def environment_info() -> dict:
    """Версии/окружение — быстро сверить "что реально задеплоено" без
    SSH на сервер (особенно DEBUG: если он вдруг True в проде — это
    security-инцидент, а не мелочь, поэтому подсвечивается отдельно)."""
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
