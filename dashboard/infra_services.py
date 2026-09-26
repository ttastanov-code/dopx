# dashboard/infra_services.py
"""Операционные метрики: Redis, Celery, PostgreSQL, расписание, лог ошибок, окружение.
Каждая функция ловит свои исключения и возвращает {"ok": False, "error": ...}.
"""
from __future__ import annotations

import logging
import re

from django.conf import settings
from django.utils import timezone
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
        reserved = inspector.reserved() or {}
        scheduled = inspector.scheduled() or {}
        stats = inspector.stats() or {}
        worker_names = list(stats.keys())
        return {
            "ok": True,
            "workers_online": len(worker_names),
            "worker_names": worker_names,
            "active_tasks": sum(len(tasks) for tasks in active.values()),
            # Взяты воркером, ждут свободного потока / отложены до времени (countdown).
            "reserved_tasks": sum(len(tasks) for tasks in reserved.values()),
            "scheduled_tasks": sum(len(tasks) for tasks in scheduled.values()),
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


_WEEKDAYS = {
    "0": "по воскресеньям", "7": "по воскресеньям", "1": "по понедельникам", "2": "по вторникам",
    "3": "по средам", "4": "по четвергам", "5": "по пятницам", "6": "по субботам",
}


def _plural(n: int, forms: tuple[str, str, str]) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return forms[0]
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return forms[1]
    return forms[2]


def _every(seconds: int) -> str:
    """«каждые 10 минут», «каждый час», «каждые 2 дня»."""
    for unit, forms in ((86400, ("день", "дня", "дней")), (3600, ("час", "часа", "часов")),
                        (60, ("минуту", "минуты", "минут")), (1, ("секунду", "секунды", "секунд"))):
        if seconds % unit == 0 and seconds >= unit:
            n = seconds // unit
            word = _plural(n, forms)
            return f"каждую {word}" if n == 1 and unit in (60, 1) else (f"каждый {word}" if n == 1 else f"каждые {n} {word}")
    return f"каждые {seconds} с"


def humanize_schedule(schedule) -> str:
    """Расписание Celery Beat человеческим языком; непонятный cron — как есть."""
    from datetime import timedelta

    from celery.schedules import crontab
    from celery.schedules import schedule as interval

    if isinstance(schedule, (int, float)):
        return _every(int(schedule))
    if isinstance(schedule, timedelta):
        return _every(int(schedule.total_seconds()))
    if isinstance(schedule, interval) and not isinstance(schedule, crontab):
        return _every(int(schedule.run_every.total_seconds()))
    if not isinstance(schedule, crontab):
        return str(schedule)

    minute, hour = str(schedule._orig_minute), str(schedule._orig_hour)
    dom, month, dow = str(schedule._orig_day_of_month), str(schedule._orig_month_of_year), str(schedule._orig_day_of_week)

    if minute.startswith("*/") and hour == "*":
        when = _every(int(minute[2:]) * 60)
    elif minute == "*" and hour == "*":
        when = "каждую минуту"
    elif hour == "*" and all(m.isdigit() for m in minute.split(",")):
        when = "каждый час в " + " и ".join(f":{int(m):02d}" for m in minute.split(","))
    elif minute.isdigit() and hour.startswith("*/"):
        when = f"{_every(int(hour[2:]) * 3600)}, в :{int(minute):02d}"
    elif minute.isdigit() and all(h.isdigit() for h in hour.split(",")):
        times = [f"{int(h):02d}:{int(minute):02d}" for h in hour.split(",")]
        when = "в " + " и ".join(times)
    else:
        return f"cron: {minute} {hour} {dom} {month} {dow}"

    if dom != "*" and dom.isdigit():
        return f"{dom}-го числа каждого месяца {when}"
    if dow != "*" and dow in _WEEKDAYS:
        return f"{_WEEKDAYS[dow]} {when}"
    if dom == "*" and dow == "*" and when.startswith("в "):
        return f"каждый день {when}"
    return when


def _task_description(task_name: str) -> str:
    """Первая строка docstring задачи — что она делает."""
    from dopx.celery import app

    task = app.tasks.get(task_name)
    doc = (getattr(task, "__doc__", "") or "").strip()
    return doc.splitlines()[0].strip() if doc else ""


def _next_run(schedule):
    from datetime import timedelta

    from celery.schedules import crontab
    from django.utils import timezone

    # Интервальные задачи отсчитываются от своего прошлого запуска, а его история не хранится —
    # честного «следующего запуска» нет, в шаблоне для них «работает постоянно».
    if not isinstance(schedule, crontab):
        return None
    now = timezone.now()
    # Ближайшее совпадение по полям cron в локальном времени (у Celery оценка путается с поясами).
    t = timezone.localtime(now).replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(32 * 24):
        dow = (t.weekday() + 1) % 7  # у cron воскресенье = 0
        if (t.month in schedule.month_of_year and t.day in schedule.day_of_month
                and dow in schedule.day_of_week and t.hour in schedule.hour):
            minutes = sorted(m for m in schedule.minute if m >= t.minute)
            if minutes:
                return t.replace(minute=minutes[0])
        t = (t + timedelta(hours=1)).replace(minute=0)
    return None


# Понятные названия задач расписания для дашборда; нет в словаре — первая строка docstring.
BEAT_TASK_TITLES = {
    "sportmonks-update-live": "Счёт и события live-матчей из Sportmonks",
    "sportmonks-update-upcoming": "Составы и изменения ближайших матчей",
    "sportmonks-resync-recent-stats": "Уточнение статистики недавно завершённых матчей",
    "sportmonks-sync-season": "Сверка календаря сезона со Sportmonks",
    "sportmonks-sync-sidelined": "Травмы и дисквалификации игроков",
    "sportmonks-sync-coach-activity": "Кто из тренеров сейчас активен",
    "sportmonks-health-check": "Проверка доступности API Sportmonks",
    "sync-error-monitor": "Проверка ошибок синхронизации и алерт админу",
    "recalculate-aggregates": "Пересчёт рейтингов недавних матчей",
    "recalculate-standings": "Пересчёт турнирной таблицы",
    "recompute-live-best-xi": "Пересчёт «Сборной DOPX» сезона",
    "recompute-round-of-week": "Пересчёт «Лучших тура»",
    "settle-trust-scores": "Уровень доверия голосующих после закрытия голосования",
    "decay-trust-scores": "Плавное снижение доверия неактивным пользователям",
    "voting-closing-reminders": "Напоминание: голосование за матч скоро закроется",
    "unfinished-evaluation-reminders": "Напоминание дооценить брошенную оценку",
    "ratings-published": "Push: голосование закрылось, рейтинги матча открыты",
    "prediction-closing-reminders": "Приглашение сделать прогноз за час до матча",
    "prediction-results": "Результаты прогнозов после матчей",
    "notification-digest-hourly": "Дайджест уведомлений на почту",
    "weekly-summary": "Персональная сводка недели пользователям",
    "staff-antifraud-digest": "Сводка антифрода для команды",
    "cleanup-old-notifications-daily": "Удаление старых прочитанных уведомлений",
    "cleanup-expired-captchas": "Удаление просроченных капч",
    "detect-referee-vote-spikes": "Антифрод: всплески оценок судей",
    "detect-vote-velocity-anomalies": "Антифрод: резкие всплески крайних оценок",
    "detect-ip-clusters": "Антифрод: группы аккаунтов с одного IP",
    "expire-stale-antifraud-flags": "Закрытие устаревших сигналов антифрода",
    "recalibrate-antifraud-thresholds": "Перенастройка порогов антифрода",
    "detect-rating-stats-divergence": "Расхождение рейтинга и статистики — команды",
    "detect-player-rating-stats-divergence": "Расхождение рейтинга и статистики — игроки",
    "detect-coach-rating-stats-divergence": "Расхождение рейтинга и статистики — тренеры",
    "verify-names-with-ai-monthly": "Проверка написания ФИО через ИИ",
    "award-monthly-champion-badge": "Награда «Чемпион месяца»",
    "revalidate-status-badges": "Перепроверка статусных достижений",
}


def beat_schedule_overview() -> list[dict]:
    """Расписание Celery Beat из settings: по-человечески, с описанием и ближайшим запуском."""
    entries = []
    for name, cfg in settings.CELERY_BEAT_SCHEDULE.items():
        schedule = cfg.get("schedule")
        task = cfg.get("task", "")
        entries.append({
            "name": name,
            "task": task,
            "description": BEAT_TASK_TITLES.get(name) or _task_description(task),
            "schedule_display": humanize_schedule(schedule),
            "next_run": _next_run(schedule),
            "is_interval": _next_run(schedule) is None,
        })
    # Сначала постоянно работающие (интервал), затем по ближайшему запуску.
    return sorted(entries, key=lambda e: (not e["is_interval"], e["next_run"] or timezone.now(), e["name"]))


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
