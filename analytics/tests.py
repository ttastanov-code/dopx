# analytics/tests.py
"""
Регрессия на баг из этой сессии: `track_event()` писал в `url_path`/
`referrer` данные о ЗАПРОСЕ К ТРЕКИНГ-ЭНДПОИНТУ (`/analytics/track/` и его
same-origin referrer), а не о реальной странице, на которой произошло
событие — из-за чего "топ страниц" в разделе "Трафик" staff-дашборда
всегда показывал один и тот же путь. Фикс — читать `properties.path`/
`properties.referrer` (клиент кладёт туда `location.pathname`/
`document.referrer`), с `request.path`/`HTTP_REFERER` только как fallback.
См. `analytics/services.py::track_event` и `static/js/analytics.js`.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.contrib.sessions.middleware import SessionMiddleware
from django.test import RequestFactory, TestCase, override_settings

from analytics.models import AnalyticsEvent, EventName
from analytics.services import hash_ip, track_event

User = get_user_model()


def _request_with_session(**meta):
    """RequestFactory не подключает SessionMiddleware сам — `track_event`
    читает `request.session.session_key`, без сессии тест упал бы
    AttributeError раньше, чем дошёл бы до проверяемого бага."""
    factory = RequestFactory()
    request = factory.get("/analytics/track/", **meta)
    SessionMiddleware(lambda r: None).process_request(request)
    request.session.save()
    request.user = AnonymousUser()
    return request


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
class TrackEventUrlPathTests(TestCase):
    """`request.path` ВСЕГДА равен '/analytics/track/' — это НЕ настоящий
    путь страницы, на которой произошло событие."""

    def test_uses_properties_path_when_provided(self):
        request = _request_with_session()
        track_event(EventName.PAGE_VIEW, request=request, properties={"path": "/matches/abc-123/"})

        event = AnalyticsEvent.objects.get()
        self.assertEqual(event.url_path, "/matches/abc-123/")
        self.assertNotEqual(event.url_path, "/analytics/track/")

    def test_falls_back_to_request_path_when_properties_path_missing(self):
        """Fallback существует для событий, где клиент не передал path
        (например, серверные события из Celery-сигналов) — НЕ для
        обычного /analytics/track/ трафика, где properties.path обязателен."""
        request = _request_with_session()
        track_event(EventName.PAGE_VIEW, request=request, properties={})

        event = AnalyticsEvent.objects.get()
        self.assertEqual(event.url_path, "/analytics/track/")


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
class TrackEventReferrerTests(TestCase):
    """`HTTP_REFERER` заголовка fetch/sendBeacon-запроса К /analytics/track/
    всегда same-origin (текущая страница сама себя) — это НЕ настоящий
    внешний referrer визита."""

    def test_uses_properties_referrer_over_same_origin_header(self):
        request = _request_with_session(HTTP_REFERER="http://127.0.0.1:8000/matches/")
        track_event(
            EventName.PAGE_VIEW, request=request,
            properties={"path": "/matches/", "referrer": "https://google.com/search?q=dopx"},
        )

        event = AnalyticsEvent.objects.get()
        self.assertEqual(event.referrer, "https://google.com/search?q=dopx")

    def test_falls_back_to_http_referer_header_when_properties_referrer_missing(self):
        request = _request_with_session(HTTP_REFERER="https://t.me/dopx_kz")
        track_event(EventName.PAGE_VIEW, request=request, properties={"path": "/matches/"})

        event = AnalyticsEvent.objects.get()
        self.assertEqual(event.referrer, "https://t.me/dopx_kz")


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
class TrackEventPrivacyTests(TestCase):
    """IP пишется ТОЛЬКО в хэшированном виде (см. analytics/services.py::
    hash_ip) — сырой адрес в БД попадать не должен ни в одно поле."""

    def test_ip_is_stored_hashed_not_raw(self):
        request = _request_with_session(REMOTE_ADDR="203.0.113.42")
        track_event(EventName.PAGE_VIEW, request=request, properties={"path": "/"})

        event = AnalyticsEvent.objects.get()
        self.assertEqual(event.ip_hash, hash_ip("203.0.113.42"))
        self.assertNotIn("203.0.113.42", event.ip_hash)


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
class TrackEventUserTests(TestCase):
    def test_authenticated_user_id_recorded(self):
        user = User.objects.create_user(username="u1", email="u1@example.com", password="pass123")
        request = _request_with_session()
        request.user = user
        track_event(EventName.EVALUATION_COMPLETED, request=request, properties={"path": "/matches/x/"})

        event = AnalyticsEvent.objects.get()
        # str() — не полагаемся на то, конвертирует ли бэкенд UUIDField
        # обратно в uuid.UUID при чтении из БД или оставляет строкой;
        # для этой проверки важна только логическая эквивалентность ID.
        self.assertEqual(str(event.user_id), str(user.id))

    def test_anonymous_request_has_no_user(self):
        request = _request_with_session()
        track_event(EventName.PAGE_VIEW, request=request, properties={"path": "/"})

        event = AnalyticsEvent.objects.get()
        self.assertIsNone(event.user_id)
