# adminbot/flags.py
"""Общие переключатели «ИИ и оповещения» без перезапуска: хранятся в «Настройках платформы», .env — значение по умолчанию."""
from __future__ import annotations

import logging

from django.conf import settings

logger = logging.getLogger(__name__)

ALERT_MODES = [("auto", "Только на проде"), ("on", "Включены везде"), ("off", "Выключены")]
PROVIDERS = [("claude", "Claude"), ("gemini", "Gemini")]

# ключ -> (подпись, описание, значение по умолчанию из .env, варианты или None для да/нет)
FLAGS = {
    "bot_ai_posts": ("Кто пишет посты в канал",
                     "ИИ — живая мини-статья по фактам из базы, тратит баланс API. "
                     "Шаблон — те же факты в готовом тексте, бесплатно.",
                     lambda: settings.ADMIN_BOT_AI_POSTS, None),
    "posts_ai_provider": ("Какой ИИ пишет посты",
                          "Если он не ответит (нет ключа, кончился баланс, сбой) — пробуем второй, затем пост соберётся по шаблону.",
                          lambda: settings.POSTS_AI_PROVIDER, PROVIDERS),
    "names_ai_provider": ("Какой ИИ проверяет ФИО",
                          "Ищет в интернете, как правильно пишется имя игрока, тренера или судьи. Не ответил — пробуем второй.",
                          lambda: settings.NAMES_AI_PROVIDER, PROVIDERS),
    "bot_ai_chat": ("Вопросы боту обычным текстом",
                    "Сотрудник пишет боту вопрос («кто лучший игрок тура?»), отвечает Claude. Каждый ответ тратит баланс API.",
                    lambda: settings.ADMIN_BOT_AI_CHAT, None),
    "bot_alerts": ("Сообщения о сбоях сервера",
                   "Бот пишет, если сайт не отвечает, лежит база или Celery, заканчивается диск и т.п.",
                   lambda: settings.ADMIN_BOT_ALERTS, ALERT_MODES),
}
# Подписи вариантов для да/нет.
BOOL_LABELS = {
    "bot_ai_posts": ("ИИ", "Шаблон, без ИИ"),
    "bot_ai_chat": ("Включены", "Выключены"),
}


def get(key: str):
    """Значение из базы, иначе из .env. База или Redis недоступны — тоже .env (алерты должны работать)."""
    default = FLAGS[key][2]()
    try:
        from core.models import get_setting

        value = get_setting(key, default)
    except Exception:
        return default
    options = FLAGS[key][3]
    return value if not options or value in dict(options) else default


def set_flag(key: str, value, user) -> None:
    from django.core.cache import cache

    from core.models import PlatformSetting

    label, description, _default, options = FLAGS[key]
    if options is None:
        stored, value_type = ("true" if value else "false"), PlatformSetting.TYPE_BOOL
    else:
        stored, value_type = str(value), PlatformSetting.TYPE_STRING
    PlatformSetting.objects.update_or_create(key=key, defaults={
        "value": stored, "value_type": value_type, "description": f"{label}. {description}", "updated_by": user})
    cache.delete(f"platform_setting:{key}")


def overview() -> list[dict]:
    """Для экранов настроек: подпись, описание, варианты, текущее значение и откуда оно."""
    from core.models import PlatformSetting

    stored = set(PlatformSetting.objects.filter(key__in=FLAGS).values_list("key", flat=True))
    out = []
    for k, (label, description, _d, options) in FLAGS.items():
        value = get(k)
        if options is None:
            on, off = BOOL_LABELS[k]
            choices = [(True, on), (False, off)]
        else:
            choices = list(options)
        out.append({"key": k, "kind": "bool" if options is None else "choice", "label": label, "description": description,
                    "options": choices, "value": value, "value_label": dict(choices).get(value, ""), "from_env": k not in stored})
    return out


def alerts_label(mode: str) -> str:
    return dict(ALERT_MODES).get(mode, mode)
