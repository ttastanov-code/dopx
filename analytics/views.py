# analytics/views.py — приём клиентских событий (sendBeacon)
from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

from analytics.services import track_event
from analytics.validators import clean_anonymous_id, is_valid_event_name, validate_properties

import logging

logger = logging.getLogger(__name__)


class ClientEventThrottle(AnonRateThrottle):
    """Отдельный лимит — вайзард даёт 15-20 событий за визит."""
    scope = "analytics_events"
    rate = "300/hour"


class TrackClientEventView(APIView):
    """Публичный эндпоинт. authentication_classes = [] — sendBeacon не шлёт CSRF."""
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [ClientEventThrottle]

    @extend_schema(exclude=True)
    # exclude=True — эндпоинт не для публичной схемы.
    def post(self, request):
        event_name = request.data.get("event_name")
        # Валидация event_name/properties — analytics/validators.py.
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
