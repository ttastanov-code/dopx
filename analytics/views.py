# analytics/views.py — приёмник клиентских событий (sendBeacon)
from __future__ import annotations

from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

from analytics.services import track_event
from analytics.validators import clean_anonymous_id, is_valid_event_name, validate_properties

import logging

logger = logging.getLogger(__name__)


class ClientEventThrottle(AnonRateThrottle):
    """Отдельный, более щедрый лимит, чем глобальный anon (100/hour) —
    на 6-шаговом вайзарде легитимный юзер легко даёт 15-20 событий за визит."""
    scope = "analytics_events"
    rate = "300/hour"


class TrackClientEventView(APIView):
    """
    Публичный (AllowAny) — события идут и от анонимов до регистрации, иначе
    не посчитать конверсию "визит → регистрация".

    authentication_classes = [] — иначе DRF наследует SessionAuthentication,
    которая требует CSRF-токен даже при AllowAny. sendBeacon (см.
    static/js/analytics.js) не умеет слать кастомные заголовки, так что с
    аутентификацией по умолчанию каждый трек от залогиненного юзера падал 403.
    """
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [ClientEventThrottle]

    def post(self, request):
        event_name = request.data.get("event_name")
        # Allow-list: EventName.choices на модели НЕ проверяется Django на
        # .objects.create() (только на full_clean()/ModelForm), так что
        # раньше сюда долетала любая строка от анонимного клиента — см.
        # analytics/validators.py.
        if not is_valid_event_name(event_name):
            return Response(status=400)

        properties = request.data.get("properties") or {}
        is_valid, reason = validate_properties(properties)
        if not is_valid:
            logger.warning(
                "Отклонено событие аналитики: event=%s reason=%s ip=%s",
                event_name, reason, request.META.get("REMOTE_ADDR"),
            )
            return Response(status=400)

        track_event(
            event_name, request=request,
            anonymous_id=clean_anonymous_id(request.data.get("anonymous_id")),
            properties=properties,
        )
        return Response(status=204)
