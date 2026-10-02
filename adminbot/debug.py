# adminbot/debug.py
"""Состояние бота для дашборда: пульс процесса runbot и журнал предупреждений/ошибок бота (в кэше)."""
from __future__ import annotations

import logging
import os
import time
import traceback

from django.conf import settings
from django.core.cache import cache

LOG_SIZE = 100
BEAT_STALE = 90      # пульса нет дольше — процесс бота не работает
STATS: dict = {"started": time.time(), "updates": 0, "last_update": None, "errors": 0}


def _env() -> str:
    return settings.ADMIN_BOT_ENV


def beat(role: str, alerts_active: list | None = None) -> None:
    """Вызывает цикл runbot: кто слушает, сколько апдейтов, активные алерты (они живут в памяти процесса)."""
    cache.set(f"adminbot:hb:{_env()}", {
        "ts": time.time(), "pid": os.getpid(), "role": role, "version": settings.APP_VERSION,
        "started": STATS["started"], "updates": STATS["updates"], "last_update": STATS["last_update"],
        "errors": STATS["errors"], "alerts": alerts_active or [],
    }, 600)


def got_update() -> None:
    STATS["updates"] += 1
    STATS["last_update"] = time.time()


def heartbeat() -> dict | None:
    hb = cache.get(f"adminbot:hb:{_env()}")
    if hb:
        hb["alive"] = time.time() - hb["ts"] < BEAT_STALE
        hb["age"] = int(time.time() - hb["ts"])
        hb["started_at"] = _dt(hb["started"])
        hb["last_update_at"] = _dt(hb["last_update"]) if hb.get("last_update") else None
        hb["uptime_h"] = round((time.time() - hb["started"]) / 3600, 1)
    return hb


def _dt(ts: float):
    from datetime import datetime, timezone as dt_tz

    return datetime.fromtimestamp(ts, tz=dt_tz.utc)


def recent_log() -> list[dict]:
    return [{**r, "at": _dt(r["ts"])} for r in reversed(cache.get(f"adminbot:log:{_env()}") or [])]


def clear_log() -> None:
    cache.delete(f"adminbot:log:{_env()}")


def _process() -> str:
    import sys

    argv = " ".join(sys.argv)
    return "бот" if "runbot" in argv else ("celery" if "celery" in argv else "сайт")


class CacheLogHandler(logging.Handler):
    """Пишет WARNING+ логгеров adminbot.* в кэш: последние LOG_SIZE записей видны в дашборде."""

    def emit(self, record):
        if getattr(record, "_adminbot_cached", False):
            return
        record._adminbot_cached = True
        try:
            text = record.getMessage()
            if record.exc_info:
                text += "\n" + "".join(traceback.format_exception(*record.exc_info))[-1500:]
            if record.levelno >= logging.ERROR:
                STATS["errors"] += 1
            key = f"adminbot:log:{_env()}"
            items = (cache.get(key) or [])[-(LOG_SIZE - 1):]
            items.append({"ts": record.created, "level": record.levelname, "logger": record.name,
                          "process": _process(), "text": text[:3000]})
            cache.set(key, items, 7 * 24 * 3600)
        except Exception:
            pass  # журнал не должен ронять бота


def install() -> None:
    log = logging.getLogger("adminbot")
    if not any(isinstance(h, CacheLogHandler) for h in log.handlers):
        handler = CacheLogHandler(level=logging.WARNING)
        log.addHandler(handler)
        if log.level == logging.NOTSET or log.level > logging.WARNING:
            log.setLevel(logging.INFO)


def telegram_info() -> dict:
    """getMe и getWebhookInfo: бот жив, нет ли вебхука (с ним long polling не работает), сколько апдейтов ждёт."""
    from . import telegram as tg

    if not tg.enabled():
        return {"ok": False, "error": "Нет токена или ADMIN_BOT_ENABLED=False"}
    try:
        me = tg.call("getMe")
        hook = tg.call("getWebhookInfo")
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}
    return {"ok": True, "username": me.get("username", ""), "name": me.get("first_name", ""),
            "webhook": hook.get("url", ""), "pending": hook.get("pending_update_count", 0),
            "last_error": hook.get("last_error_message", "")}
