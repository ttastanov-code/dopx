# dashboard/server_tools.py
"""Администрирование сервера из дашборда (раздел «Сервер»): логи, кэш, очереди, база, бэкапы, сайт.
Только суперпользователь; каждое действие пишется в аудит во вьюхе."""
from __future__ import annotations

import logging
import os
import re
import signal
from datetime import datetime, timedelta
from pathlib import Path

from django.conf import settings
from django.core.cache import caches
from django.db import connection
from django.utils import timezone

logger = logging.getLogger(__name__)

BACKUPS_DIR = Path(getattr(settings, "BACKUPS_DIR", settings.BASE_DIR / "backups"))
LOG_TAIL_MAX_BYTES = 2 * 2**20
LOG_LEVELS = ("ERROR", "CRITICAL", "WARNING", "INFO")
CELERY_QUEUES = ("celery", "realtime")


# --- Логи ---

def list_logs() -> list[dict]:
    """Файлы логов: logs/*.log* и logs/deploy/*.log, новые сверху."""
    root = Path(settings.LOGS_DIR)
    files = [p for p in root.glob("*.log*") if p.is_file()] + [p for p in (root / "deploy").glob("*.log") if p.is_file()]
    rows = [{"name": str(p.relative_to(root)), "size": p.stat().st_size,
             "modified": datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.get_current_timezone())} for p in files]
    return sorted(rows, key=lambda r: r["modified"], reverse=True)


def log_path(name: str) -> Path | None:
    """Путь только к файлу из list_logs(): защита от ../ в параметре."""
    allowed = {row["name"] for row in list_logs()}
    return Path(settings.LOGS_DIR) / name if name in allowed else None


def read_log(name: str, lines: int = 300, query: str = "", level: str = "") -> dict:
    """Последние строки лога (с конца файла, до 2 МБ) с фильтром по тексту и уровню."""
    path = log_path(name)
    if path is None:
        return {"ok": False, "error": "Нет такого файла логов", "lines": []}
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - LOG_TAIL_MAX_BYTES))
        text = fh.read().decode("utf-8", errors="replace")
    rows = text.splitlines()[1 if size > LOG_TAIL_MAX_BYTES else 0:]
    if level in LOG_LEVELS:
        rows = [r for r in rows if level in r]
    if query:
        q = query.lower()
        rows = [r for r in rows if q in r.lower()]
    return {"ok": True, "lines": [{"text": r, "level": _line_level(r)} for r in rows[-lines:]], "total": len(rows)}


def _line_level(line: str) -> str:
    for level in ("CRITICAL", "ERROR", "WARNING"):
        if level in line:
            return level.lower()
    return ""


# --- Кэш и очереди ---

def cache_overview() -> dict:
    """Redis: память, ключи; очереди Celery."""
    out = {"ok": False, "queues": []}
    try:
        import redis

        client = redis.Redis.from_url(settings.CACHES["default"]["LOCATION"], socket_connect_timeout=2, socket_timeout=2)
        info = client.info()
        out.update(ok=True, memory=info.get("used_memory_human"), peak=info.get("used_memory_peak_human"),
                   keys=client.dbsize(), uptime_days=round(info.get("uptime_in_seconds", 0) / 86400, 1))
    except Exception as exc:  # noqa: BLE001 — кэш без Redis (тесты, ноутбук)
        out["error"] = str(exc)[:200]
    out["queues"] = queue_lengths()
    return out


def queue_lengths() -> list[dict]:
    from dopx.celery import app

    rows = []
    try:
        with app.connection_for_read() as conn:
            for name in CELERY_QUEUES:
                try:
                    # Число потребителей Redis не отдаёт — показываем только длину очереди.
                    rows.append({"name": name, "count": conn.default_channel.queue_declare(name, passive=True)[1]})
                except Exception:  # noqa: BLE001 — очереди ещё нет
                    rows.append({"name": name, "count": 0})
    except Exception as exc:  # noqa: BLE001
        logger.warning("queue_lengths: %s", exc)
    return rows


def clear_cache(kind: str) -> str:
    """pages — страницы и live-блоки (новая версия данных); aggregates — кэш рейтингов; all — всё."""
    from core.live import bump_data_version

    if kind == "pages":
        bump_data_version()
        return "Кэш страниц сброшен: страницы и live-блоки пересоберутся при следующем открытии."
    if kind == "aggregates":
        caches["aggregates"].clear()
        bump_data_version()
        return "Кэш рейтингов очищен."
    if kind == "all":
        for alias in settings.CACHES:
            caches[alias].clear()
        return "Весь кэш очищен. Сессии в базе, входы не сбросятся; лимиты и блокировки задач обнулены."
    raise ValueError(kind)


def purge_queue(name: str) -> int:
    """Удаляет ждущие задачи очереди (выполняющиеся не трогает)."""
    if name not in CELERY_QUEUES:
        raise ValueError(name)
    from dopx.celery import app

    with app.connection_for_write() as conn:
        return conn.default_channel.queue_purge(name) or 0


# --- Сайт ---

def reload_web() -> tuple[bool, str]:
    """Плавная перезагрузка gunicorn: SIGHUP мастеру, воркеры сменяются по очереди, сайт не падает."""
    ppid = os.getppid()
    try:
        cmdline = Path(f"/proc/{ppid}/cmdline").read_bytes().decode(errors="replace")
    except OSError:
        cmdline = ""
    if "gunicorn" not in cmdline:
        return False, "Сайт запущен не через gunicorn (ноутбук) — перезагрузите dev-сервер вручную."
    os.kill(ppid, signal.SIGHUP)
    return True, "Сайт перезагружается: воркеры сменятся за несколько секунд, без простоя."


# --- База данных ---

def db_overview() -> dict:
    with connection.cursor() as cur:
        cur.execute("select pg_size_pretty(pg_database_size(current_database())), "
                    "(select count(*) from pg_stat_activity where datname = current_database())")
        size, conns = cur.fetchone()
        cur.execute("""
            select relname, n_live_tup, n_dead_tup, pg_size_pretty(pg_total_relation_size(relid)),
                   pg_total_relation_size(relid), last_autovacuum, last_vacuum
            from pg_stat_user_tables order by pg_total_relation_size(relid) desc limit 15""")
        tables = [{"name": r[0], "rows": r[1], "dead": r[2], "size": r[3], "bytes": r[4],
                   "vacuumed": r[5] or r[6], "bloated": r[2] > max(1000, r[1] * 0.2)} for r in cur.fetchall()]
        cur.execute("""
            select pid, now() - query_start, state, left(query, 300), usename
            from pg_stat_activity
            where datname = current_database() and pid <> pg_backend_pid() and state <> 'idle'
              and query_start < now() - interval '5 seconds'
            order by query_start limit 20""")
        slow = [{"pid": r[0], "duration": r[1], "state": r[2], "query": r[3], "user": r[4]} for r in cur.fetchall()]
    return {"size": size, "connections": conns, "tables": tables, "slow": slow,
            "bloated_count": sum(t["bloated"] for t in tables)}


def cancel_query(pid: int) -> bool:
    with connection.cursor() as cur:
        cur.execute("select pg_cancel_backend(%s)", [pid])
        return bool(cur.fetchone()[0])


def vacuum_table(table: str) -> None:
    """VACUUM ANALYZE таблицы из списка пользовательских (имя проверено по каталогу)."""
    with connection.cursor() as cur:
        cur.execute("select 1 from pg_stat_user_tables where relname = %s", [table])
        if not cur.fetchone() or not re.fullmatch(r"[a-z0-9_]+", table):
            raise ValueError(table)
        cur.execute(f'VACUUM (ANALYZE) "{table}"')


def clear_expired_sessions() -> int:
    from django.contrib.sessions.models import Session

    deleted, _ = Session.objects.filter(expire_date__lt=timezone.now()).delete()
    return deleted


# --- Бэкапы ---

def backups_overview() -> dict:
    """Файлы бэкапов (папка backups/ смонтирована в контейнер только для чтения) и итог последнего прогона."""
    if not BACKUPS_DIR.is_dir():
        return {"ok": False, "error": f"Папка бэкапов {BACKUPS_DIR} не смонтирована", "files": []}
    files = sorted((p for p in BACKUPS_DIR.rglob("*") if p.is_file() and p.suffix in (".gz", ".sql", ".dump")),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    rows = [{"name": str(p.relative_to(BACKUPS_DIR)), "size": p.stat().st_size,
             "modified": datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.get_current_timezone())} for p in files[:30]]
    latest_db = next((r for r in rows if "pre-deploy" not in r["name"] and r["name"].endswith((".sql.gz", ".dump"))), None)
    age = timezone.now() - latest_db["modified"] if latest_db else None
    log_tail = read_log("backup.log", lines=12) if log_path("backup.log") else {"lines": []}
    return {"ok": True, "files": rows, "latest": latest_db, "stale": age is None or age > timedelta(hours=26),
            "log": log_tail["lines"]}


# --- Режим обслуживания ---

MAINTENANCE_SETTING = "maintenance_mode"


def maintenance_enabled() -> bool:
    from core.models import get_setting

    return bool(get_setting(MAINTENANCE_SETTING, False))


def set_maintenance(on: bool) -> None:
    from django.core.cache import cache

    from core.models import PlatformSetting

    PlatformSetting.objects.update_or_create(key=MAINTENANCE_SETTING, defaults={
        "value": "true" if on else "false", "value_type": PlatformSetting.TYPE_BOOL,
        "description": "Режим обслуживания: посетители видят заглушку, сотрудники работают как обычно.",
    })
    cache.delete(f"platform_setting:{MAINTENANCE_SETTING}")
