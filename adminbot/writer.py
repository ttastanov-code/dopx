# adminbot/writer.py
"""Тексты постов канала: ИИ (Claude или Gemini) пишет мини-статью по проверенным фактам, иначе — шаблон (план Б).
Ответ модели проходит проверку: каждое число в тексте должно быть в фактах."""
from __future__ import annotations

import json
import logging
import re

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

# После ошибки ключа/баланса не дёргаем API столько секунд — сразу шаблоны.
BREAKER_TTL = 6 * 3600
BREAKER_KEY = "adminbot:ai_breaker"
# Мелкие числа и шкалы оценок (из 10) и драмы (из 100).
ALWAYS_OK_NUMBERS = {"0", "1", "2", "3", "10", "100"}

SYSTEM = (
    "Ты пишешь посты для Telegram-канала DOPX — независимого рейтинга казахстанского футбола (КПЛ) "
    "по оценкам болельщиков. Пиши по-русски, живо и по делу, как спортивный журналист: сильный заголовок, "
    "история матча, цифры, мнение трибун. Без канцелярита и без восторгов на пустом месте.\n\n"
    "Жёсткие правила:\n"
    "1. Используй ТОЛЬКО факты из JSON. Не придумывай события, цифры, цитаты, причины и составы. "
    "Нет факта — не пиши об этом. Числа, минуты и счёт пиши ровно как в фактах (например, 90+5), ничего не пересчитывай.\n"
    "2. Формат — HTML для Telegram: только <b> и <i>. Без Markdown, без ссылок, без хештегов (их добавят).\n"
    "3. Первая строка — заголовок в <b>, с одним подходящим эмодзи в начале.\n"
    "4. Абзацы короткие, 2–5 абзацев, эмодзи — в начале абзацев, не больше одного на абзац.\n"
    "5. Длина — {length}.\n"
    "6. Выведи только текст поста. Никогда не упоминай JSON, «факты», «данные» и чего в них нет — "
    "просто не пиши об этом. Никаких заметок, оговорок и пояснений от себя."
)

GUIDES = {
    "review": "Разбор матча. Сначала история (камбэк, поздний гол, сенсация по прогнозам, разгром — что есть в фактах), "
              "затем ход матча с минутами голов, цифры статистики, герой и антигерой по оценкам болельщиков, "
              "как разошлись фанаты двух команд, что изменилось в таблице, цитата эксперта если есть.",
    "preview": "Превью тура. Что на кону в главных матчах: места в таблице, форма, очные встречи. Прогнозы болельщиков "
               "и цитаты экспертов — только если они есть в фактах; если нет, вообще не упоминай их. "
               "Закончи призывом сделать прогноз.",
    "round": "Итоги тура: игрок тура, сборная, самый спорный судья, самый драматичный матч, главная сенсация, "
             "сколько болельщиков оценило тур.",
    "controversy": "Спорный момент недели: почему трибуны разошлись в оценке судьи, цифры разрыва. "
                   "Закончи вопросом к читателю — ниже будет опрос.",
    "number": "Рубрика «Цифра недели»: одна главная цифра крупно в заголовке и короткая история вокруг неё.",
}


def _breaker(provider: str) -> str:
    return f"{BREAKER_KEY}:{provider}"


def providers() -> list[str]:
    """Порядок попыток: выбранный, затем второй — если у них есть ключ и они не на паузе."""
    from core import llm

    from .flags import get

    first = get("posts_ai_provider")
    return [p for p in (first, llm.other(first)) if llm.configured(p) and not cache.get(_breaker(p))]


def ai_enabled() -> bool:
    from .flags import get

    return bool(get("bot_ai_posts") and providers())


def _norm(n: str) -> str:
    n = n.replace(",", ".")
    return n.rstrip("0").rstrip(".") if "." in n else n.lstrip("0") or "0"


def _numbers(text: str) -> set[str]:
    return {_norm(n) for n in re.findall(r"\d+(?:[.,]\d+)?", text)}


# Признаки служебного текста модели в посте («в JSON нет…», «Заметка: …»).
META_MARKERS = ("json", "в фактах", "нет данных", "данных нет", "заметка", "не придумыва", "у нас нет",
                "отсутству", "не располага", "нет информации")


def facts_ok(text: str, facts: dict) -> bool:
    """Нет ли в тексте чисел, которых нет в фактах (защита от выдумок)."""
    lowered = text.lower()
    if any(m in lowered for m in META_MARKERS):
        logger.warning("adminbot writer: в тексте служебные пометки модели — беру шаблон")
        return False
    allowed = _numbers(json.dumps(facts, ensure_ascii=False)) | ALWAYS_OK_NUMBERS
    extra = _numbers(re.sub(r"<[^>]+>", "", text)) - allowed
    if extra:
        logger.warning("adminbot writer: в тексте числа не из фактов %s — беру шаблон", sorted(extra)[:5])
    return not extra


def _trip(provider: str, reason: str) -> None:
    from core import llm

    if not cache.get(_breaker(provider)):
        logger.warning("adminbot writer: %s недоступен (%s) — %d ч не используем", llm.label(provider), reason, BREAKER_TTL // 3600)
    cache.set(_breaker(provider), reason, BREAKER_TTL)


def _bold_title(text: str) -> str:
    """Первая строка — заголовок: если ИИ не выделил его, выделяем сами."""
    title, sep, rest = text.partition("\n")
    return text if "<b>" in title or not title else f"<b>{title}</b>{sep}{rest}"


def ask_ai(kind: str, facts: dict, length: str) -> tuple[str, str] | None:
    """(текст, провайдер) или None: выключено, оба недоступны, отказ или проверка фактов не прошла."""
    from core import llm

    from .channel import clean_html

    if not ai_enabled():
        return None
    prompt = f"{GUIDES[kind]}\n\nФакты (JSON):\n{json.dumps(facts, ensure_ascii=False, default=str)}"
    for provider in providers():
        try:
            text = llm.generate(provider, SYSTEM.format(length=length), prompt)
        except llm.LLMError as e:
            if e.kind in ("auth", "billing"):
                _trip(provider, "закончился баланс" if e.kind == "billing" else "ключ не принят")
            else:
                logger.warning("adminbot writer: %s не ответил: %s", llm.label(provider), str(e)[:200])
            continue
        text = _bold_title(clean_html(text.strip()))
        if text and facts_ok(text, facts):
            return text, provider
    return None


def ask_claude(kind: str, facts: dict, length: str) -> str | None:
    result = ask_ai(kind, facts, length)
    return result[0] if result else None


def write(kind: str, facts: dict, fallback, length: str = "600–1100 знаков") -> tuple[str, bool]:
    """(текст, написан ли ИИ). fallback() — шаблон из тех же фактов."""
    result = ask_ai(kind, facts, length)
    return (result[0], True) if result else (fallback(), False)


def status() -> dict:
    """Для дашборда: как сейчас пишутся тексты."""
    from core import llm

    from .flags import get

    from core.safe_mode import allowed

    order = [llm.label(p) for p in providers()]
    if not allowed("ai"):
        summary = "Посты в канал пишутся по шаблону, без ИИ: ИИ выключен безопасным режимом ноутбука."
    elif not get("bot_ai_posts"):
        summary = "Посты в канал пишутся по шаблону, без ИИ."
    elif not order:
        summary = "Выбран ИИ, но он недоступен (нет ключа или закончился баланс) — пока посты пишутся по шаблону."
    else:
        summary = f"Посты в канал пишет ИИ: {order[0]}" + (f", запасной — {order[1]}" if len(order) > 1 else "") \
            + ". Если ИИ не ответит — пост соберётся по шаблону."
    return {"summary": summary, "posts": get("bot_ai_posts"), "chat": get("bot_ai_chat"), "provider": llm.label(get("posts_ai_provider")),
            "order": [llm.label(p) for p in providers()], "model": settings.ADMIN_BOT_AI_MODEL,
            "paused": {llm.label(p): cache.get(_breaker(p)) for p, _l in llm.PROVIDERS if cache.get(_breaker(p))},
            "key": any(llm.configured(p) for p, _l in llm.PROVIDERS)}
