# parsers/name_ai.py
"""Проверка кириллического ФИО через ИИ с веб-поиском: Gemini или Claude (выбор — в «Настройках бота»).
Результат только предлагается — применяется после подтверждения staff в дашборде."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

SYSTEM = "Ты проверяешь написание имён для спортивного сайта и отвечаешь строго одним JSON-объектом."


@dataclass
class NameVerificationResult:
    """Результат проверки. ok=False — техническая ошибка вызова;
    ok=True с confidence="low" — ИИ не уверен.
    """

    ok: bool
    first_name: str = ""
    last_name: str = ""
    confidence: str = ""  # "high" | "medium" | "low"
    reasoning: str = ""
    matches_current: bool = False
    error: str = ""
    provider: str = ""


def _order() -> list[str]:
    from adminbot.flags import get
    from core import llm

    first = get("names_ai_provider")
    return [p for p in (first, llm.other(first)) if llm.configured(p)]


def is_configured() -> bool:
    return bool(_order())


def provider_label() -> str:
    """Кто сейчас проверяет ФИО — для подписей в дашборде."""
    from core import llm

    order = _order()
    return llm.label(order[0]) if order else "ИИ"


def verify_name(
    entity_label: str, first_name: str, last_name: str,
    *, team_name: str = "", extra_context: str = "",
) -> NameVerificationResult:
    """entity_label — «игрока»/«судьи»/«тренера». team_name/extra_context — для отличия тёзок.
    Выбранный провайдер не ответил технически — пробуем второй."""
    from core import llm

    order = _order()
    if not order:
        return NameVerificationResult(ok=False, error="Нет ключа ни для Gemini, ни для Claude (GEMINI_API_KEY / ANTHROPIC_API_KEY)")
    prompt = _build_prompt(entity_label, first_name, last_name, team_name, extra_context)
    errors = []
    for provider in order:
        try:
            text = llm.generate(provider, SYSTEM, prompt, web_search=True, max_tokens=8000)
        except llm.LLMError as exc:
            # Предупреждение: дальше пробуем второй провайдер; ошибка — только если не ответил никто.
            logger.warning("%s: проверка ФИО не удалась (%s %r %r): %s", llm.label(provider), entity_label, first_name, last_name, exc)
            errors.append(f"{llm.label(provider)}: {exc}"[:400])
            continue
        result = _parse_text(text, entity_label, first_name, last_name)
        result.provider = provider
        if result.ok:
            return result
        errors.append(f"{llm.label(provider)}: {result.error}")
    logger.error("ИИ: проверка ФИО не удалась ни у одного провайдера (%s %r %r): %s", entity_label, first_name, last_name, " | ".join(errors))
    return NameVerificationResult(ok=False, error=" | ".join(errors))


def _build_prompt(entity_label: str, first_name: str, last_name: str, team_name: str, extra_context: str) -> str:
    context_line = f" Клуб/команда: {team_name}." if team_name else ""
    extra_line = f" {extra_context}" if extra_context else ""
    return (
        f"Ты помогаешь казахстанскому спортивному сайту DOPX (Казахстанская Премьер-Лига, "
        f"KPL) проверить правильное написание ФИО кириллицей для {entity_label}. "
        f'Текущее написание в нашей базе данных: "{first_name} {last_name}".'
        f"{context_line}{extra_line}\n\n"
        "Найди через поиск в интернете (казахстанские спортивные СМИ, официальный сайт "
        "КПЛ/клуба, sports.kz, vesti.kz, championat.asia, Transfermarkt на русском языке "
        "и подобные источники), как РЕАЛЬНО пишут имя этого конкретного человека кириллицей "
        "в контексте казахстанского футбола. Если человек не находится или источники "
        "противоречат друг другу — честно укажи низкую уверенность (confidence: \"low\"), "
        "НЕ выдумывай написание. Если несколько источников сходятся на одном написании — "
        "уверенность \"high\". Если только один слабый источник — \"medium\".\n\n"
        "Ответь СТРОГО одним JSON-объектом, без markdown-разметки и пояснений вокруг:\n"
        '{"first_name": "...", "last_name": "...", "confidence": "high|medium|low", '
        '"matches_current": true|false, "reasoning": "коротко, 1-2 предложения, на какие '
        'источники опирался или почему не уверен"}'
    )


def _parse_text(text: str, entity_label: str, first_name: str, last_name: str) -> NameVerificationResult:
    # Модель иногда оборачивает JSON в ```json ... ``` или добавляет текст вокруг.
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.MULTILINE).strip()
    if not cleaned.startswith("{"):
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        cleaned = match.group(0) if match else cleaned

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        logger.warning("ИИ: не удалось распарсить JSON-ответ (%s %r %r): текст=%r", entity_label, first_name, last_name, text[:500])
        return NameVerificationResult(ok=False, error=f"не удалось распарсить ответ как JSON: {exc}")

    if not isinstance(parsed, dict):
        return NameVerificationResult(ok=False, error=f"ответ распарсился, но это не объект: {type(parsed).__name__}")

    result_first = str(parsed.get("first_name") or "").strip()
    result_last = str(parsed.get("last_name") or "").strip()
    confidence = str(parsed.get("confidence") or "low").strip().lower()
    if confidence not in ("high", "medium", "low"):
        confidence = "low"
    reasoning = str(parsed.get("reasoning") or "").strip()
    matches_current = bool(parsed.get("matches_current"))

    if not result_first and not result_last:
        return NameVerificationResult(ok=False, error="ИИ не вернул ни имени, ни фамилии")

    return NameVerificationResult(
        ok=True, first_name=result_first, last_name=result_last,
        confidence=confidence, reasoning=reasoning, matches_current=matches_current,
    )
