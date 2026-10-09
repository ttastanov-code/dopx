# adminbot/alerts.py
"""Алерты проекта и сервера. Проверяет процесс бота раз в минуту — работает и при упавшем Celery.
Шлём после двух неудачных проверок подряд, повтор раз в час, затем «восстановлено»."""
from __future__ import annotations

import logging
import os
import re
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

import requests
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

CONFIRM_CHECKS = 2
REMIND_EVERY = 60 * 60
MUTE_FOR = 60 * 60
# Пороги.
DISK_FREE_MIN = 0.10
MEMORY_FREE_MIN = 0.08
QUEUE_MAX = 300
ERRORS_PER_10MIN = 25
SYNC_STALE_HOURS = 12
BACKUP_MAX_AGE_HOURS = 30
LATENCY_P95_MAX = 2.0
LATENCY_MIN_REQUESTS = 50
_LOG_TS = re.compile(r"^ERROR\s+(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


@dataclass
class Problem:
    key: str
    title: str
    detail: str = ""
    critical: bool = False


def enabled() -> bool:
    from .flags import get

    mode = get("bot_alerts")
    return mode == "on" or (mode == "auto" and settings.ADMIN_BOT_ENV == "prod")


# ---------------- Проверки: каждая возвращает Problem или None
def check_site():
    url = settings.ADMIN_BOT_HEALTH_URL
    if not url:
        return None
    host = (settings.ALLOWED_HOSTS or ["localhost"])[0]
    try:
        r = requests.get(url, timeout=8, headers={"Host": host, "X-Forwarded-Proto": "https"})
        if r.status_code >= 500:
            return Problem("site", "Сайт не отвечает", f"/healthz/ вернул {r.status_code}", True)
        if r.status_code >= 400:
            return Problem("site", "Сайт отвечает ошибкой", f"/healthz/ вернул {r.status_code}", True)
    except requests.RequestException as e:
        return Problem("site", "Сайт недоступен", str(e)[:200], True)
    return None


def check_db():
    from core.health import _check_db

    return None if _check_db() else Problem("db", "База данных недоступна", critical=True)


def check_cache():
    from core.health import _check_cache

    return None if _check_cache() else Problem("cache", "Redis/кэш недоступен", critical=True)


def check_celery():
    from core.health import HEARTBEAT_STALE_SECONDS, celery_heartbeat_age

    age = celery_heartbeat_age()
    if age is None or age > HEARTBEAT_STALE_SECONDS:
        return Problem("celery", "Celery не работает",
                       "Пульса не было" if age is None else f"Последний пульс {age // 60} мин назад", True)
    return None


def check_services():
    """Боты и воркер realtime: молчат по пульсу (core.heartbeat). Сайт — в check_site, Celery-воркер — в check_celery."""
    from core import heartbeat

    bad = [r for r in heartbeat.overview()
           if r["name"] in ("fan_bot", "celery_realtime") and r["status"] in ("down", "stale")]
    if not bad:
        return None
    names = ", ".join(r["label"] for r in bad)
    return Problem("services", f"Не отвечает: {names}", "Docker перезапустит контейнер сам. Если не поможет, откройте Дашборд → Системный статус.")


def check_queue():
    from dashboard.infra_services import _redis_stats

    depth = _redis_stats().get("queue_depth") or 0
    return Problem("queue", "Очередь задач растёт", f"Ждут выполнения: {depth}") if depth > QUEUE_MAX else None


def check_disk():
    d = shutil.disk_usage(settings.BASE_DIR)
    if d.free / d.total < DISK_FREE_MIN:
        return Problem("disk", "Заканчивается место на диске", f"Свободно {d.free / 1e9:.1f} из {d.total / 1e9:.0f} ГБ", True)
    return None


def check_memory():
    try:
        info = {}
        with open("/proc/meminfo") as fh:
            for line in fh:
                k, v = line.split(":", 1)
                info[k] = int(v.split()[0])
        free = info["MemAvailable"] / info["MemTotal"]
    except (OSError, KeyError, ValueError, ZeroDivisionError):
        return None  # не Linux — пропускаем
    if free < MEMORY_FREE_MIN:
        return Problem("memory", "Мало свободной памяти", f"Доступно {free:.0%}")
    return None


def check_load():
    try:
        load = os.getloadavg()[1]
    except OSError:
        return None
    cores = os.cpu_count() or 1
    return Problem("load", "Сервер перегружен", f"Нагрузка {load:.1f} при {cores} ядрах") if load > cores * 2 else None


def check_errors():
    path = settings.LOGS_DIR / "errors.log"
    if not path.exists():
        return None
    try:
        size = path.stat().st_size
        with open(path, "r", errors="replace") as fh:
            if size > 200_000:
                fh.seek(size - 200_000)
                fh.readline()
            lines = fh.read().splitlines()
    except OSError:
        return None
    since = datetime.now() - timedelta(minutes=10)
    recent = []
    for line in lines:
        m = _LOG_TS.match(line)
        if m and datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S") >= since:
            recent.append(line)
    if len(recent) >= ERRORS_PER_10MIN:
        sample = recent[-1].split(" ", 4)[-1][:160]
        return Problem("errors", "Всплеск ошибок", f"{len(recent)} за 10 минут. Последняя: {sample}")
    return None


def check_sync():
    from parsers.models import ParserSyncRun
    from parsers.sportmonks.tasks import _sync_enabled

    if not _sync_enabled():
        return None  # межсезонье или нет подписки: синк выключен намеренно
    runs = list(ParserSyncRun.objects.filter(source="sportmonks").order_by("-started_at")[:3])
    if not runs:
        return None
    if timezone.now() - runs[0].started_at > timedelta(hours=SYNC_STALE_HOURS):
        return Problem("sync", "Sportmonks давно не синхронизировался",
                       f"Последний запуск {timezone.localtime(runs[0].started_at):%d.%m %H:%M}")
    if len(runs) == 3 and all(r.errors and not r.updated for r in runs):
        return Problem("sync", "Синхронизация Sportmonks с ошибками", f"Три запуска подряд: ошибок {runs[0].errors}")
    return None


def check_backup():
    folder = settings.BASE_DIR / "backups"
    if settings.ADMIN_BOT_ENV != "prod" or not folder.exists():
        return None
    files = list(folder.glob("db_*.sql.gz"))
    if not files:
        return Problem("backup", "Нет ежедневных бэкапов базы", "В папке backups/ нет файлов db_*.sql.gz")
    age = (time.time() - max(f.stat().st_mtime for f in files)) / 3600
    if age > BACKUP_MAX_AGE_HOURS:
        return Problem("backup", "Бэкап базы устарел", f"Последний {age:.0f} ч назад")
    return backup_restore_problem()


def backup_restore_problem():
    """Итог еженедельной проверки восстановления (scripts/verify_backup.sh пишет в logs/backup.log)."""
    path = settings.LOGS_DIR / "backup.log"
    try:
        with open(path, "r", errors="replace") as fh:
            fh.seek(max(0, path.stat().st_size - 20_000))
            tail = fh.read()
    except OSError:
        return None
    bad, good = tail.rfind("БЭКАП НЕ ГОДИТСЯ"), tail.rfind("Бэкап годный.")
    if bad > good:
        return Problem("backup", "Бэкап не восстанавливается", "Еженедельная проверка провалилась — см. logs/backup.log")
    return None


def check_latency():
    from .latency import p95

    value, total = p95(5)
    if value is not None and total >= LATENCY_MIN_REQUESTS and value > LATENCY_P95_MAX:
        shown = "больше 10" if value == float("inf") else f"до {value:g}"
        return Problem("latency", "Сайт тормозит", f"p95 {shown} с за 5 минут · запросов {total}")
    return None


def recent_errors(limit: int = 8) -> list[str]:
    """Последние строки ERROR из errors.log."""
    path = settings.LOGS_DIR / "errors.log"
    if not path.exists():
        return []
    try:
        size = path.stat().st_size
        with open(path, "r", errors="replace") as fh:
            if size > 100_000:
                fh.seek(size - 100_000)
                fh.readline()
            lines = [l for l in fh.read().splitlines() if l.startswith("ERROR")]
    except OSError:
        return []
    return lines[-limit:]


def check_integrity():
    """Итог ежедневной проверки целостности данных (сама проверка тяжёлая — тут только чтение из кэша)."""
    from core.integrity import last_report

    report = last_report()
    if not report or not report["errors"]:
        return None
    worst = [f for f in report["findings"] if f["severity"] == "error" and f["count"]]
    detail = "; ".join(f"{f['title']}: {f['count']}" for f in worst[:3])
    return Problem("integrity", "Данные на сайте не сходятся", detail + ". Дашборд → Здоровье данных.")


CHECKS = [check_site, check_latency, check_db, check_cache, check_celery, check_services, check_queue, check_disk, check_memory,
          check_load, check_errors, check_sync, check_backup, check_integrity]


def current_problems() -> list[Problem]:
    found = []
    for check in CHECKS:
        try:
            p = check()
        except Exception as e:   # сама проверка упала — тоже повод знать
            logger.warning("adminbot alerts: %s failed: %s", check.__name__, e)
            p = Problem(check.__name__, f"Проверка {check.__name__} не выполнилась", str(e)[:160])
        if p:
            found.append(p)
    return found


# ---------------- Состояние и отправка
# В памяти процесса бота, а не в Redis: алерт «Redis недоступен» тоже должен дойти.
STATE: dict[str, dict] = {}
MUTED: dict[str, float] = {}
LAST_RECIPIENTS: list[int] = []


def mute(key: str) -> None:
    MUTED[key] = time.time() + MUTE_FOR


def muted(key: str) -> bool:
    return MUTED.get(key, 0) > time.time()


def active() -> list[dict]:
    return [st for st in STATE.values() if st.get("sent")]


def _buttons(key: str) -> list:
    from .handlers import ENV, cb

    rows = [[("🩺 Сервер", cb("status")), ("📜 Ошибки", cb("errors"))]]
    if key in ("celery", "queue") and ENV == "prod":
        rows.append([("🔄 Перезапустить воркеры", cb("celery_restart"))])
    rows.append([("🔕 На 1 час", cb("mute", key))])
    return rows


def _fallback_send(text, rows):
    """Отправка без базы: последним известным получателям."""
    from . import telegram as tg

    for chat_id in LAST_RECIPIENTS:
        tg.send(chat_id, text, rows)


def _remember_recipients():
    global LAST_RECIPIENTS
    from .notify import recipients

    try:
        LAST_RECIPIENTS = recipients("system_status", topic="incidents")
    except Exception:
        pass


def run_checks(send=None) -> list[str]:
    """Одна проверка: обновить состояние, разослать новые алерты, напоминания и восстановления.
    Без send — через инциденты (дежурный, «Беру»); если база лежит — напрямую."""
    from . import incidents
    from .handlers import esc, header

    if send is None:
        _remember_recipients()
    now = time.time()
    problems = {p.key: p for p in current_problems()}
    events = []

    for key, p in problems.items():
        st = STATE.setdefault(key, {"since": now, "seen": 0, "sent": 0, "key": key})
        st.update(seen=st["seen"] + 1, title=p.title, detail=p.detail, critical=p.critical)
        due = st["seen"] >= CONFIRM_CHECKS and (not st["sent"] or now - st["sent"] >= REMIND_EVERY)
        if due and not muted(key):
            icon = "🔴" if p.critical else "🟠"
            again = " (всё ещё)" if st["sent"] else ""
            since = datetime.fromtimestamp(st["since"], tz=timezone.get_current_timezone())
            text = header() + f"{icon} <b>{esc(p.title)}</b>{again}\n{esc(p.detail)}\nС {since:%H:%M}"
            if send:
                send(text, _buttons(key))
            else:
                try:
                    if not st["sent"]:
                        incidents.open_incident(f"alert:{key}", p.title, text, _buttons(key))
                    elif not incidents.acked(f"alert:{key}"):
                        _fallback_send(text, _buttons(key))
                except Exception:
                    logger.warning("adminbot alerts: инциденты недоступны, шлю напрямую", exc_info=True)
                    _fallback_send(text, _buttons(key))
            st["sent"] = now
            events.append(f"alert:{key}")

    for key in [k for k in STATE if k not in problems]:
        st = STATE.pop(key)
        if st.get("sent"):
            minutes = max(1, int((now - st["since"]) / 60))
            text = header() + f"✅ <b>Восстановлено:</b> {esc(st.get('title', key))}\nДлилось ~{minutes} мин."
            if send:
                send(text, None)
            else:
                try:
                    from . import telegram as tg
                    for chat in incidents.resolve(f"alert:{key}", "✅ Восстановлено") or LAST_RECIPIENTS:
                        tg.send(chat, text)
                except Exception:
                    _fallback_send(text, None)
            events.append(f"ok:{key}")
    return events
