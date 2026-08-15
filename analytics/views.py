# analytics/views.py — приёмник клиентских событий (sendBeacon)
from __future__ import annotations

from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

from analytics.services import track_event


class ClientEventThrottle(AnonRateThrottle):
    """Отдельный, более щедрый лимит, чем глобальный anon (100/hour) —
    на 6-шаговом вайзарде легитимный юзер легко даёт 15-20 событий за визит."""
    scope = "analytics_events"
    rate = "300/hour"


class TrackClientEventView(APIView):
    """
    Публичный (AllowAny) — события идут и от анонимов ДО регистрации,
    иначе не посчитать конверсию "визит → регистрация".

    ИСПРАВЛЕНО: `authentication_classes = []` — без этого DRF наследует
    глобальный default (включает SessionAuthentication), которая для
    залогиненных пользователей ПРИНУДИТЕЛЬНО проверяет CSRF-токен ещё ДО
    permission_classes — независимо от AllowAny. `static/js/analytics.js`
    шлёт события через `navigator.sendBeacon`/`fetch(keepalive)`, которые
    физически не могут приложить X-CSRFToken (sendBeacon не поддерживает
    кастомные заголовки вообще), поэтому КАЖДЫЙ трек от залогиненного
    юзера падал 403 (см. лог: "Forbidden: /analytics/track/" на каждой
    загрузке страницы). Отключаем аутентификацию целиком для этого вью —
    он и не должен знать, кто звонит через сессию, у него свой anonymous_id.
    """
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [ClientEventThrottle]

    def post(self, request):
        event_name = request.data.get("event_name")
        if not event_name:
            return Response(status=400)
        track_event(
            event_name, request=request,
            anonymous_id=request.data.get("anonymous_id"),
            properties=request.data.get("properties") or {},
        )
        return Response(status=204)
