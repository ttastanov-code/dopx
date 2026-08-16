# analytics/services.py
"""Единая точка входа для трекинга — используется ВЕЗДЕ вместо
`AnalyticsEvent.objects.create()` напрямую."""
from __future__ import annotations

import hashlib
import uuid
from typing import Any

from django.conf import settings
from django.http import HttpRequest

from analytics.models import EventName
from analytics.tasks import persist_event_task


def hash_ip(ip: str) -> str:
    """Необратимый хэш IP — в БД никогда не попадает сырой адрес (PII)."""
    return hashlib.sha256(f"{ip}{settings.SECRET_KEY}".encode()).hexdigest()


def track_event(
    event_name: EventName | str,
    *,
    request: HttpRequest | None = None,
    user: Any = None,
    anonymous_id: str | uuid.UUID | None = None,
    properties: dict[str, Any] | None = None,
) -> None:
    """
    :param request: если передан — из него достаются IP/UA/referrer/session/UTM.
    :param user: явный пользователь, если request.user недоступен (сигналы, Celery).
    :param properties: произвольные JSON-сериализуемые доп. данные события.

    Запись всегда асинхронная (fire-and-forget через Celery) — событие
    аналитики никогда не должно быть узким местом request-response цикла,
    особенно на 6-шаговом вайзарде оценки.
    """
    payload: dict[str, Any] = {"event_name": str(event_name), "properties": properties or {}}

    resolved_user = user or (getattr(request, "user", None) if request else None)
    if resolved_user is not None and getattr(resolved_user, "is_authenticated", False):
        payload["user_id"] = str(resolved_user.id)

    if anonymous_id:
        payload["anonymous_id"] = str(anonymous_id)

    if request is not None:
        from core.utils import get_client_ip  # локальный импорт — избегаем циклических зависимостей

        # request — это запрос К /analytics/track/, не к странице события,
        # поэтому request.path/HTTP_REFERER всегда указывают на сам трекинг-
        # эндпоинт. Реальные path/referrer клиент кладёт в properties
        # (см. static/js/analytics.js), request.* — только fallback.
        session_id = request.session.session_key or ""
        payload["session_id"] = session_id
        properties_in = payload["properties"]
        payload["url_path"] = str(properties_in.get("path") or request.path)[:500]
        payload["referrer"] = str(properties_in.get("referrer") or request.META.get("HTTP_REFERER", ""))[:500]
        payload["user_agent"] = request.META.get("HTTP_USER_AGENT", "")[:300]
        ip = get_client_ip(request)
        if ip:
            payload["ip_hash"] = hash_ip(ip)
        for key in ("utm_source", "utm_medium", "utm_campaign"):
            value = request.GET.get(key, "")
            if value:
                payload[key] = value[:100]

    persist_event_task.delay(payload)
