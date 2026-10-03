# core/heartbeat.py
"""Пульс процессов (сайт, Celery, боты): отметка в Redis + файл для healthcheck Docker.
Дашборд «Сервисы», алерты бота и autoheal читают отсюда, кто жив, какой версии и что сломалось."""
from __future__ import annotations

import os
import socket
import time
from pathlib import Path

from django.conf import settings
from django.core.cache import cache

STARTED = time.time()
ALIVE_FILE = Path("/tmp/dopx-alive")

# имя: (подпись, через сколько секунд тишины — «не отвечает»)
SERVICES = {
    "web": ("Сайт (gunicorn)", 600),
    "celery_worker": ("Celery: фоновые задачи", 240),
    "celery_realtime": ("Celery: live-матчи и пуши", 240),
    "celery_beat": ("Celery beat: расписание", 240),
    "admin_bot": ("Бот команды", 240),
    "fan_bot": ("Бот болельщиков", 240),
}


def beat(name: str, **info) -> None:
    """Отметиться: процесс жив. info — что показать на странице (счётчики, последняя ошибка)."""
    try:
        cache.set(f"hb:{name}", {"ts": time.time(), "pid": os.getpid(), "host": socket.gethostname(),
                                 "version": settings.APP_VERSION, "started": STARTED, **info}, 24 * 3600)
    except Exception:
        pass  # Redis лёг — файл ниже всё равно обновим, контейнер не перезапустят зря
    try:
        ALIVE_FILE.touch()
    except OSError:
        pass


def request_restart(name: str) -> None:
    """Кнопка «Перезапустить»: процесс увидит флаг в своём цикле и завершится, Docker поднимет его заново."""
    cache.set(f"hb:restart:{name}", time.time(), 3600)


def restart_requested(name: str) -> bool:
    asked = cache.get(f"hb:restart:{name}")
    return bool(asked and asked > STARTED)


def enabled(name: str) -> bool:
    """Выключенные намеренно (нет токена бота) не считаются упавшими."""
    if name == "fan_bot":
        return bool(settings.FAN_BOT_TOKEN)
    if name == "admin_bot":
        from adminbot import telegram as tg
        return tg.enabled()
    return True


def overview() -> list[dict]:
    """Статус каждого сервиса: ok / stale / down / off; versions_differ — не обновился после деплоя."""
    now = time.time()
    rows = []
    for name, (label, stale_after) in SERVICES.items():
        hb = cache.get(f"hb:{name}") or {}
        age = int(now - hb["ts"]) if hb.get("ts") else None
        if not enabled(name):
            status = "off"
        elif age is None:
            status = "down"
        elif age > stale_after:
            status = "down" if age > stale_after * 3 else "stale"
        else:
            status = "ok"
        rows.append({
            "name": name, "label": label, "status": status, "age": age, "hb": hb,
            "uptime": int(now - hb["started"]) if hb.get("started") else None,
            "outdated": bool(hb.get("version")) and hb.get("version") != settings.APP_VERSION and status == "ok",
            "restartable": name in ("admin_bot", "fan_bot", "celery_worker", "celery_realtime"),
        })
    return rows
