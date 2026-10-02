# core/llm.py
"""Единый вызов ИИ: Claude (Anthropic SDK) или Gemini (REST). Ошибки приводятся к LLMError с видом причины."""
from __future__ import annotations

import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

PROVIDERS = [("claude", "Claude"), ("gemini", "Gemini")]
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
TIMEOUT = 60
# Серверный веб-поиск Claude: сколько раз за запрос модель может искать.
WEB_SEARCH_USES = 5


class LLMError(Exception):
    """kind: auth — ключ; billing — баланс/квота; temporary — сеть, перегрузка; refused — отказ;
    disabled — безопасный режим; other."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


def label(provider: str) -> str:
    return dict(PROVIDERS).get(provider, provider)


def configured(provider: str) -> bool:
    """Есть ключ и ИИ не выключен безопасным режимом."""
    from core.safe_mode import allowed

    return allowed("ai") and bool(settings.ANTHROPIC_API_KEY if provider == "claude" else settings.GEMINI_API_KEY)


def other(provider: str) -> str:
    return "gemini" if provider == "claude" else "claude"


def generate(provider: str, system: str, prompt: str, *, web_search: bool = False, max_tokens: int = 4000) -> str:
    """Текст ответа. web_search — модель сама ищет в интернете (для проверки ФИО)."""
    from core.safe_mode import allowed

    if not allowed("ai"):
        raise LLMError("disabled", "безопасный режим ноутбука: ИИ выключен")
    if not configured(provider):
        raise LLMError("auth", f"нет ключа {label(provider)}")
    return _claude(system, prompt, web_search, max_tokens) if provider == "claude" else _gemini(system, prompt, web_search, max_tokens)


def _claude(system: str, prompt: str, web_search: bool, max_tokens: int) -> str:
    import anthropic

    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY, timeout=float(TIMEOUT), max_retries=2)
    request = {"model": settings.ADMIN_BOT_AI_MODEL, "max_tokens": max_tokens, "system": system,
               "output_config": {"effort": "low"}, "betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
    if web_search:
        request["tools"] = [{"type": "web_search_20260209", "name": "web_search", "max_uses": WEB_SEARCH_USES}]
    messages = [{"role": "user", "content": prompt}]
    try:
        for _ in range(4):
            response = client.beta.messages.create(messages=messages, **request)
            # Длинный серверный поиск ставит ход на паузу — продолжаем тем же контентом.
            if response.stop_reason != "pause_turn":
                break
            messages.append({"role": "assistant", "content": response.content})
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
        raise LLMError("auth", str(e)) from None
    except anthropic.BadRequestError as e:
        text = str(e).lower()
        raise LLMError("billing" if "credit" in text or "billing" in text else "other", str(e)) from None
    except (anthropic.RateLimitError, anthropic.APIConnectionError) as e:
        raise LLMError("temporary", str(e)) from None
    except anthropic.APIStatusError as e:
        raise LLMError("temporary" if e.status_code >= 500 else "other", str(e)) from None
    if response.stop_reason == "refusal":
        raise LLMError("refused", "Claude отказался отвечать")
    if response.stop_reason == "max_tokens":
        raise LLMError("other", "ответ Claude обрезан по длине")
    return "\n".join(b.text for b in response.content if b.type == "text").strip()


def _gemini(system: str, prompt: str, web_search: bool, max_tokens: int) -> str:
    payload = {"contents": [{"parts": [{"text": prompt}]}], "systemInstruction": {"parts": [{"text": system}]},
               "generationConfig": {"temperature": 0.0 if web_search else 0.7, "maxOutputTokens": max_tokens}}
    if web_search:
        payload["tools"] = [{"google_search": {}}]
    try:
        response = requests.post(GEMINI_URL.format(model=settings.GEMINI_MODEL), params={"key": settings.GEMINI_API_KEY},
                                 json=payload, timeout=TIMEOUT)
    except requests.RequestException as e:
        raise LLMError("temporary", f"{type(e).__name__}") from None
    if response.status_code in (401, 403):
        raise LLMError("auth", f"Gemini {response.status_code}: {response.text[:200]}")
    if response.status_code == 429:
        # Квота: суточный лимит — как баланс, минутный — временно.
        kind = "billing" if "perday" in response.text.lower().replace(" ", "").replace("_", "") else "temporary"
        raise LLMError(kind, f"Gemini 429: {response.text[:300]}")
    if response.status_code >= 500:
        raise LLMError("temporary", f"Gemini {response.status_code}")
    if response.status_code >= 400:
        raise LLMError("other", f"Gemini {response.status_code}: {response.text[:300]}")
    try:
        data = response.json()
        candidates = data.get("candidates") or []
        if not candidates:
            reason = (data.get("promptFeedback") or {}).get("blockReason")
            raise LLMError("refused", f"пустой ответ Gemini{f', blockReason={reason}' if reason else ''}")
        parts = (candidates[0].get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts).strip()
    except (ValueError, AttributeError, TypeError) as e:
        raise LLMError("other", f"неожиданный ответ Gemini: {e}") from None
    if not text:
        raise LLMError("other", f"Gemini вернул пустой текст (finishReason={candidates[0].get('finishReason')})")
    return text
