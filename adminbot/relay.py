# adminbot/relay.py
"""Ретрансляция апдейтов от слушателя (прод) агенту (ноутбук): очередь в кэше,
агент забирает её GET /bot/relay/ с секретным ключом."""
from __future__ import annotations

import hmac
import time

import requests
from django.conf import settings
from django.core.cache import cache

QUEUE_TTL = 15 * 60
AGENT_TTL = 45          # агент считается в сети столько секунд после последнего опроса
LISTENER_TTL = 90       # слушатель обновляет метку каждый цикл getUpdates
LONG_POLL = 10


def _seq_key(env):
    return f"adminbot:relay:{env}:seq"


def _read_key(env):
    return f"adminbot:relay:{env}:read"


def push(env: str, update: dict) -> None:
    cache.add(_seq_key(env), 0, None)
    n = cache.incr(_seq_key(env))
    cache.set(f"adminbot:relay:{env}:{n}", update, QUEUE_TTL)


def pop_all(env: str, wait: float = LONG_POLL) -> list[dict]:
    """Все непрочитанные апдейты; если пусто — ждём до wait секунд."""
    deadline = time.monotonic() + wait
    while True:
        seq = cache.get(_seq_key(env)) or 0
        read = cache.get(_read_key(env)) or 0
        if seq > read:
            items = [cache.get(f"adminbot:relay:{env}:{n}") for n in range(read + 1, seq + 1)]
            cache.set(_read_key(env), seq, None)
            return [u for u in items if u]
        if time.monotonic() >= deadline:
            return []
        time.sleep(.4)


def mark_agent(env: str) -> None:
    cache.set(f"adminbot:agent:{env}", time.time(), AGENT_TTL)


def agent_online(env: str) -> bool:
    return cache.get(f"adminbot:agent:{env}") is not None


def mark_listener(env: str) -> None:
    cache.set("adminbot:listener", env, LISTENER_TTL)


def listener_env() -> str | None:
    return cache.get("adminbot:listener")


def secret_ok(given: str | None) -> bool:
    secret = settings.ADMIN_BOT_RELAY_SECRET or ""
    return len(secret) >= 24 and bool(given) and hmac.compare_digest(secret, given)


# ---------- Сторона агента (ноутбук)
def _hub(path: str, **params):
    resp = requests.get(settings.ADMIN_BOT_HUB_URL.rstrip("/") + path, params=params,
                        headers={"X-Relay-Key": settings.ADMIN_BOT_RELAY_SECRET}, timeout=LONG_POLL + 10)
    resp.raise_for_status()
    return resp.json()


def hub_status() -> dict | None:
    """Что слушает хаб (прод); None — хаб не настроен или недоступен."""
    if not settings.ADMIN_BOT_HUB_URL or not settings.ADMIN_BOT_RELAY_SECRET:
        return None
    try:
        return _hub("/bot/relay/", ping=1, env=settings.ADMIN_BOT_ENV)
    except (requests.RequestException, ValueError):
        return None


def fetch(env: str) -> list[dict]:
    return _hub("/bot/relay/", env=env).get("updates", [])
