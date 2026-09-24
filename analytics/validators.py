# analytics/validators.py
"""Валидация данных для /analytics/track/ (эндпоинт публичный):
event_name — только из EventName, properties — ограниченный JSON.
"""
from __future__ import annotations

import json
import uuid

from analytics.models import EventName

MAX_PROPERTIES_KEYS = 20
MAX_PROPERTY_KEY_LENGTH = 100
MAX_PROPERTY_STRING_VALUE_LENGTH = 500
MAX_PROPERTIES_JSON_BYTES = 4096
# Максимальная глубина вложенности properties.
MAX_PROPERTIES_DEPTH = 2


def is_valid_event_name(event_name: str) -> bool:
    """event_name — только из EventName.values."""
    return isinstance(event_name, str) and event_name in EventName.values


def _check_depth(value, depth: int = 0) -> bool:
    if depth > MAX_PROPERTIES_DEPTH:
        return False
    if isinstance(value, dict):
        return all(_check_depth(v, depth + 1) for v in value.values())
    if isinstance(value, list):
        return all(_check_depth(v, depth + 1) for v in value)
    return True


def validate_properties(properties) -> tuple[bool, str]:
    """(валидно, причина отказа для лога)."""
    if not isinstance(properties, dict):
        return False, "properties must be an object"
    if len(properties) > MAX_PROPERTIES_KEYS:
        return False, f"too many keys (> {MAX_PROPERTIES_KEYS})"
    for key, value in properties.items():
        if not isinstance(key, str) or not key or len(key) > MAX_PROPERTY_KEY_LENGTH:
            return False, "invalid key"
        if isinstance(value, str) and len(value) > MAX_PROPERTY_STRING_VALUE_LENGTH:
            return False, "string value too long"
    if not _check_depth(properties):
        return False, f"nesting exceeds depth {MAX_PROPERTIES_DEPTH}"
    try:
        serialized = json.dumps(properties, ensure_ascii=False)
    except (TypeError, ValueError):
        return False, "not JSON-serializable"
    if len(serialized.encode("utf-8")) > MAX_PROPERTIES_JSON_BYTES:
        return False, f"payload exceeds {MAX_PROPERTIES_JSON_BYTES} bytes"
    return True, ""


def clean_anonymous_id(anonymous_id) -> str | None:
    """Невалидный anonymous_id отбрасываем."""
    if not anonymous_id:
        return None
    try:
        return str(uuid.UUID(str(anonymous_id)))
    except (ValueError, TypeError, AttributeError):
        return None
