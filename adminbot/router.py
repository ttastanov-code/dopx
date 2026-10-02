# adminbot/router.py
"""Слушатель: решает, в каком окружении обработать апдейт. Кнопки несут env в callback_data,
код 2FA уходит в окружение, которое его запросило."""
from __future__ import annotations

from django.core.cache import cache

from . import handlers, relay
from . import telegram as tg

SELECT_TTL = 30 * 24 * 3600
LAST_TTL = 5 * 60


def _selected(tid) -> str:
    return cache.get(f"adminbot:sel:{tid}") or handlers.ENV


def _tid_chat(update):
    msg = update.get("message")
    q = update.get("callback_query")
    src = msg or q or {}
    chat = (msg or (q or {}).get("message") or {}).get("chat", {})
    return src.get("from", {}).get("id"), chat.get("type")


def available(env: str) -> bool:
    return env == handlers.ENV or relay.agent_online(env)


def route(update: dict) -> str | None:
    """Обработать или переслать апдейт; возвращает окружение-получатель (для тестов и логов)."""
    if "my_chat_member" in update:
        from . import guard
        guard.on_membership(update)
        return None
    tid, chat_type = _tid_chat(update)
    if not tid or chat_type != "private":
        return None
    q = update.get("callback_query")
    msg = update.get("message")

    # Переключатель окружения — обрабатывает сам слушатель.
    if q and q.get("data", "").startswith("env|"):
        target = q["data"].split("|", 1)[1]
        if target not in handlers.ENV_LABEL:
            tg.answer(q["id"])
            return None
        if not available(target):
            tg.answer(q["id"], f"{handlers.ENV_LABEL[target]} сейчас не в сети", alert=True)
            return None
        cache.set(f"adminbot:sel:{tid}", target, SELECT_TTL)
        cache.set(f"adminbot:last:{tid}", target, LAST_TTL)
        tg.answer(q["id"], f"Окружение: {handlers.ENV_LABEL[target]}")
        # Меню нужного окружения: как будто пользователь написал /menu.
        menu_update = {"update_id": update.get("update_id"), "message": {
            "chat": q["message"]["chat"], "from": q["from"], "text": "/menu"}}
        return _dispatch(target, menu_update, tid)

    if q:
        letter = q.get("data", "").split("|", 1)[0]
        target = handlers.LETTER_ENV.get(letter)
        if not target:
            tg.answer(q["id"])
            return None
        cache.set(f"adminbot:last:{tid}", target, LAST_TTL)
    else:
        # Код 2FA и текст после кнопки (ответ, пост) — туда, где нажали кнопку.
        last = cache.get(f"adminbot:last:{tid}")
        target = last or _selected(tid)
    return _dispatch(target, update, tid)


def _dispatch(target: str, update: dict, tid) -> str | None:
    if target == handlers.ENV:
        handlers.handle(update)
        return target
    if relay.agent_online(target):
        relay.push(target, update)
        return target
    q = update.get("callback_query")
    if q:
        tg.answer(q["id"], f"{handlers.ENV_LABEL[target]} сейчас не в сети", alert=True)
    else:
        tg.send(tid, f"{handlers.ENV_LABEL[target]} сейчас не в сети: ноутбук выключен или бот там не запущен.",
                [handlers.env_row()])
    return None
