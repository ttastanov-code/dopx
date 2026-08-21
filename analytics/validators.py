# analytics/validators.py
"""
Валидация публичных данных для /analytics/track/.

БАГ, КОТОРЫЙ ТУТ БЫЛ (закрыт 2026-08-21, см. docs/BACKLOG.md): эндпоинт —
AllowAny и без аутентификации (sendBeacon не умеет кастомные заголовки, см.
analytics/views.py), поэтому event_name и properties фактически приходят
от анонимного клиента без какого-либо контроля. `EventName.choices` на
модели (analytics/models.py) создавал ложное чувство защиты — Django НЕ
проверяет choices при `.objects.create()`, только при `full_clean()`/
ModelForm, так что ЛЮБАЯ строка долетала до БД как event_name. `properties`
был вообще неограниченным JSONField — один недобросовестный клиент мог
годами раздувать таблицу произвольными вложенными структурами и портить
любую агрегацию по event_name.
"""
from __future__ import annotations

import json
import uuid

from analytics.models import EventName

MAX_PROPERTIES_KEYS = 20
MAX_PROPERTY_KEY_LENGTH = 100
MAX_PROPERTY_STRING_VALUE_LENGTH = 500
MAX_PROPERTIES_JSON_BYTES = 4096
# properties — плоский набор метаданных события ("шаг вайзарда", "канал
# шеринга" и т.п.), не произвольный JSON-документ. Глубина 2 покрывает
# редкий вложенный случай (например, {"context": {"step": 3}}) и при этом
# не даёт прислать сколь угодно вложенную структуру ради раздувания записи.
MAX_PROPERTIES_DEPTH = 2


def is_valid_event_name(event_name: str) -> bool:
    """Allow-list — событие обязано быть одним из EventName.values, иначе
    воронка через полгода зарастает произвольными вариантами написания."""
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
    """Возвращает (валидно, причина отказа для лога — наружу не отдаём,
    чтобы не подсказывать атакующему точные границы)."""
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
    """Мусорный/невалидный anonymous_id молча отбрасываем (не критичное
    поле, ронять из-за него всё событие незачем), а не пропускаем как есть
    — иначе он долетел бы до БД в анонимной колонке произвольной строкой."""
    if not anonymous_id:
        return None
    try:
        return str(uuid.UUID(str(anonymous_id)))
    except (ValueError, TypeError, AttributeError):
        return None
