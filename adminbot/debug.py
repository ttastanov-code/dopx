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
        # Ошибки — по журналу с запуска процесса, а не счётчиком в памяти бота:
        # иначе после «Очистить журнал» цифра остаётся, а журнал пуст.
        hb["errors"] = sum(1 for r in cache.get(f"adminbot:log:{_env()}") or []
                           if r["level"] in ("ERROR", "CRITICAL") and r["ts"] >= hb["started"])
    return hb


def _dt(ts: float):
    from datetime import datetime, timezone as dt_tz

    return datetime.fromtimestamp(ts, tz=dt_tz.utc)


# Понятное описание частых ошибок для журнала в дашборде (технический текст — под «подробнее»).
_HUMAN = (
    ("NameResolutionError", "Нет интернета: не удалось найти сервер Telegram"),
    ("Failed to resolve", "Нет интернета: не удалось найти сервер Telegram"),
    ("Connection reset by peer", "Связь с Telegram оборвалась"),
    ("Connection aborted", "Связь с Telegram оборвалась"),
    ("Read timed out", "Telegram долго не отвечал"),
    ("ConnectTimeout", "Не удалось подключиться к Telegram"),
    ("Redis недоступен", "Redis не запущен"),
    ("Error 61 connecting", "Redis не запущен"),
    ("бота слушает другой процесс", "Бот запущен ещё в одном окне — этот ждёт"),
    ("Too Many Requests", "Telegram попросил подождать (слишком много запросов)"),
    ("Forbidden", "Telegram запретил действие (бота заблокировали или нет прав)"),
)


def humanize(text: str) -> str:
    for needle, label in _HUMAN:
        if needle in (text or ""):
            return label
    first = (text or "").strip().splitlines()[0] if text else ""
    return first[:160]


def recent_log() -> list[dict]:
    """Записи журнала, новые сначала; одинаковые подряд склеены в одну с числом повторов."""
    out: list[dict] = []
    for r in reversed(cache.get(f"adminbot:log:{_env()}") or []):
        human = humanize(r.get("text", ""))
        if out and out[-1]["human"] == human and out[-1]["level"] == r.get("level"):
            out[-1]["count"] += 1
            out[-1]["first_at"] = _dt(r["ts"])
            continue
        out.append({**r, "at": _dt(r["ts"]), "first_at": None, "human": human, "count": 1})
    return out


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
            from .telegram import redact

            text = record.getMessage()
            if record.exc_info:
                text += "\n" + "".join(traceback.format_exception(*record.exc_info))[-1500:]
            text = redact(text)
            if record.levelno >= logging.ERROR:
                STATS["errors"] += 1
            key = f"adminbot:log:{_env()}"
            items = (cache.get(key) or [])[-(LOG_SIZE - 1):]
            items.append({"ts": record.created, "level": record.levelname, "logger": record.name,
                          "process": _process(), "text": text[:3000]})
            cache.set(key, items, 7 * 24 * 3600)
        except Exception:
            pass  # журнал не должен ронять бота


class RedactTokenFilter(logging.Filter):
    """Вырезает токен бота из любой записи логгеров adminbot.* — до файлов, журнала и Sentry."""

    def filter(self, record):
        from .telegram import redact

        msg = record.getMessage()
        clean = redact(msg)
        if clean != msg:
            record.msg, record.args = clean, ()
        return True


def install() -> None:
    log = logging.getLogger("adminbot")
    # Фильтр логгера не видит записи дочерних логгеров — ставим и на все обработчики.
    loggers = [logging.getLogger()] + [l for l in logging.Logger.manager.loggerDict.values() if isinstance(l, logging.Logger)]
    for target in [log] + [h for l in loggers for h in l.handlers]:
        if not any(isinstance(f, RedactTokenFilter) for f in target.filters):
            target.addFilter(RedactTokenFilter())
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
