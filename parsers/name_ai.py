# parsers/name_ai.py
"""
Проверка правильного кириллического написания ФИО игрока/судьи/тренера
через Gemini API (2026-09-22, прямая просьба пользователя после жалобы
"Сергий Малий" вместо "Сергий Малый" — механическая транслитерация
(parsers/sportmonks/translit.py) в принципе не может быть 100% верной,
реальное написание нужно ПРОВЕРЯТЬ по внешнему источнику, не гадать по
буквам ещё точнее).

ПОЧЕМУ GEMINI, А НЕ АВТОМАТИЗАЦИЯ ЧАТ-ИНТЕРФЕЙСА: пользователь сначала
предлагал "открывать чат ChatGPT Go/Gemini/Claude Pro и слать запрос" —
это прямое нарушение пользовательского соглашения любого из этих
потребительских продуктов (автоматизация веб-чата ботом запрещена),
технически хрупко (headless-браузер, живая сессия, капча) и не то, на чём
стоит строить рабочую фичу сайта. Осознанный выбор — официальный Gemini
API (aistudio.google.com), бесплатный тариф, БЕЗ привязки к личной
подписке пользователя.

ПОЧЕМУ БЕЗ ВНЕШНЕЙ БИБЛИОТЕКИ google-generativeai: Gemini API — обычный
REST/JSON эндпоинт, `requests` (уже используется в parsers/sportmonks/
client.py — тот же принцип) достаточно, не тянем новую зависимость ради
одного эндпоинта.

ПОЧЕМУ РЕЗУЛЬТАТ НЕ ПРИМЕНЯЕТСЯ АВТОМАТИЧЕСКИ: явное решение пользователя
("всегда через ручное подтверждение в дашборде") — LLM тоже может
ошибиться/не найти редкого игрока, автоприменение непроверенной догадки
поверх уже испорченного механической транслитерацией имени рискует
заменить одну ошибку на другую, никем не замеченную. Эта функция только
ВОЗВРАЩАЕТ предложение — запись в базу и в ConfirmedNameCorrection делает
dashboard/views.py::names_review_approve ПОСЛЕ явного клика staff (см.
parsers/models.py::NameVerificationSuggestion).

ВАЖНО ПРО ИНСТРУМЕНТ ПОИСКА: код ниже посылает Gemini `tools: [{"google_
search": {}}]` — актуальное имя server-side инструмента веб-поиска для
моделей семейства Gemini 1.5-002+/2.0/2.5. Название/доступность
инструментов у Google периодически меняются, а живого API-ключа в
песочнице этой сессии нет — проверить вживую было нельзя. Если после
получения реального ключа Gemini будет возвращать ошибку про неизвестный
инструмент — см. актуальную документацию на ai.google.dev и поправьте
GOOGLE_SEARCH_TOOL ниже (единственное место, где инструмент описан).
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

GEMINI_ENDPOINT_TEMPLATE = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
REQUEST_TIMEOUT_SECONDS = 30

# См. докстринг модуля выше про возможную смену имени инструмента.
GOOGLE_SEARCH_TOOL = {"google_search": {}}


@dataclass
class NameVerificationResult:
    """Результат ОДНОЙ проверки. `ok=False` — вызов не удался технически
    (нет ключа, сеть, невалидный JSON) — это НЕ то же самое, что "Gemini
    не нашёл человека": в последнем случае `ok=True`, но `confidence="low"`
    и `reasoning` объясняет, что источники не найдены/противоречивы —
    staff в дашборде видит оба случая по-разному (ошибка вызова vs честное
    "не уверен")."""

    ok: bool
    first_name: str = ""
    last_name: str = ""
    confidence: str = ""  # "high" | "medium" | "low"
    reasoning: str = ""
    matches_current: bool = False
    error: str = ""


def is_configured() -> bool:
    return bool(getattr(settings, "GEMINI_API_KEY", ""))


def verify_name(
    entity_label: str, first_name: str, last_name: str,
    *, team_name: str = "", extra_context: str = "",
) -> NameVerificationResult:
    """entity_label — человекочитаемое "игрока"/"судьи"/"тренера" (падеж
    как в parsers/sportmonks/importers.py::_resolve_cyrillic_name, чтобы
    промпт читался естественно). team_name/extra_context — любой
    дополнительный контекст, который поможет отличить полного тёзку
    (клуб, страна, позиция и т.п.) — не обязателен."""
    if not is_configured():
        return NameVerificationResult(ok=False, error="GEMINI_API_KEY не задан в настройках (см. dopx/settings.py)")

    prompt = _build_prompt(entity_label, first_name, last_name, team_name, extra_context)
    url = GEMINI_ENDPOINT_TEMPLATE.format(model=settings.GEMINI_MODEL)
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "tools": [GOOGLE_SEARCH_TOOL],
        "generationConfig": {"temperature": 0.0},
    }

    try:
        response = requests.post(url, params={"key": settings.GEMINI_API_KEY}, json=payload, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        logger.error("Gemini: запрос не удался (%s %r %r): %s", entity_label, first_name, last_name, exc)
        # 2026-09-22: для 429/4xx/5xx достаём тело ответа — там у Gemini
        # обычно лежит status ("RESOURCE_EXHAUSTED" и т.п.) и quotaMetric,
        # по которому видно, упёрлись в per-minute или per-day лимит
        # (просто "429 Too Many Requests" из str(exc) этого не говорит —
        # снаружи неотличимо, увеличивать --delay бесполезно или нет).
        detail = ""
        response = getattr(exc, "response", None)
        if response is not None:
            try:
                detail = f" | body: {response.text[:500]}"
            except Exception:
                pass
        return NameVerificationResult(ok=False, error=f"{type(exc).__name__}: {exc}{detail}")
    except ValueError as exc:  # response.json() — невалидный JSON от сервера
        logger.error("Gemini: невалидный JSON в HTTP-ответе (%s %r %r): %s", entity_label, first_name, last_name, exc)
        return NameVerificationResult(ok=False, error=f"невалидный JSON HTTP-ответа: {exc}")

    return _parse_response(data, entity_label, first_name, last_name)


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


def _parse_response(data: dict, entity_label: str, first_name: str, last_name: str) -> NameVerificationResult:
    try:
        candidates = data.get("candidates") or []
    except AttributeError:
        return NameVerificationResult(ok=False, error=f"неожиданный формат ответа Gemini: {data!r:.500}")

    if not candidates:
        block_reason = ((data.get("promptFeedback") or {}).get("blockReason")) if isinstance(data, dict) else None
        suffix = f", blockReason={block_reason}" if block_reason else ""
        return NameVerificationResult(ok=False, error=f"пустой ответ от Gemini (candidates=[]){suffix}")

    try:
        parts = (candidates[0].get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts).strip()
    except (AttributeError, TypeError, IndexError) as exc:
        return NameVerificationResult(ok=False, error=f"неожиданная структура candidates[0] у Gemini: {exc}")

    if not text:
        finish_reason = candidates[0].get("finishReason") if candidates else None
        suffix = f" (finishReason={finish_reason})" if finish_reason else ""
        return NameVerificationResult(ok=False, error=f"Gemini вернул пустой текст{suffix}")

    # Модель иногда оборачивает JSON в ```json ... ``` несмотря на явную просьбу не делать этого.
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.MULTILINE).strip()

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        logger.warning("Gemini: не удалось распарсить JSON-ответ (%s %r %r): текст=%r", entity_label, first_name, last_name, text)
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
        return NameVerificationResult(ok=False, error="Gemini не вернул ни имени, ни фамилии")

    return NameVerificationResult(
        ok=True, first_name=result_first, last_name=result_last,
        confidence=confidence, reasoning=reasoning, matches_current=matches_current,
    )
