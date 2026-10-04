# core/redact.py
"""Вырезает секреты из текста до логов, журнала бота и Sentry: токены ботов, api_token Sportmonks, ключи Claude и Gemini.
Токены часто сидят в URL запроса, а URL попадает в текст сетевых ошибок."""
from __future__ import annotations

import logging
import re

PATTERNS = (
    (re.compile(r"bot\d+:[A-Za-z0-9_-]{20,}"), "bot<токен>"),
    (re.compile(r"(api_token=)[^&\s'\"]+", re.I), r"\1<токен>"),
    (re.compile(r"([?&]key=)[^&\s'\"]+"), r"\1<ключ>"),
    (re.compile(r"sk-ant-[A-Za-z0-9_-]{10,}"), "sk-ant-<ключ>"),
    (re.compile(r"AIza[0-9A-Za-z_-]{30,}"), "AIza<ключ>"),
)


def redact(text) -> str:
    text = str(text)
    for pattern, repl in PATTERNS:
        text = pattern.sub(repl, text)
    return text


class SecretsFilter(logging.Filter):
    """Фильтр для всех обработчиков логов: чистит сообщение и текст трейсбека."""

    def filter(self, record):
        msg = record.getMessage()
        clean = redact(msg)
        if clean != msg:
            record.msg, record.args = clean, ()
        if record.exc_info and not record.exc_text:
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
        return True


def scrub_event(event, hint=None):
    """before_send / before_breadcrumb для Sentry: все строки события без секретов."""
    def walk(value):
        if isinstance(value, str):
            return redact(value)
        if isinstance(value, dict):
            return {k: walk(v) for k, v in value.items()}
        if isinstance(value, list):
            return [walk(v) for v in value]
        return value
    return walk(event)
