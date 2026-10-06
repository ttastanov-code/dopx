# core/tests_guest_cache.py
"""Кэш публичных страниц для гостей: свой CSRF-токен каждому, вошедшим и тостам — без кэша."""
from django.core.cache import cache
from django.http import HttpResponse
from django.middleware.csrf import get_token
from django.template import Context, Template
from django.test import RequestFactory, TestCase, override_settings
from django.contrib.auth.models import AnonymousUser
from django.contrib.sessions.backends.cache import SessionStore

from dopx.middleware import GuestPageCacheMiddleware

PAGE = Template('<body hx-headers=\'{"X-CSRFToken": "{{ t }}"}\'><form><input name="csrfmiddlewaretoken" value="{{ t }}"></form>{{ n }}</body>')


@override_settings(GUEST_PAGE_CACHE_SECONDS=45)
class GuestPageCacheTests(TestCase):
    def setUp(self):
        cache.clear()
        self.calls = 0

        def view(request):
            self.calls += 1
            return HttpResponse(PAGE.render(Context({"t": get_token(request), "n": self.calls})))

        self.mw = GuestPageCacheMiddleware(view)

    def _get(self, path="/matches/", user=None, cookies=None):
        request = RequestFactory().get(path)
        request.user = user or AnonymousUser()
        request.session = SessionStore()
        request.COOKIES.update(cookies or {})
        return request, self.mw(request)

    def test_second_guest_gets_cached_page_with_own_token(self):
        _, first = self._get()
        req2, second = self._get()
        self.assertEqual(self.calls, 1)
        self.assertEqual(second["X-Guest-Cache"], "hit")
        html = second.content.decode()
        self.assertNotIn(GuestPageCacheMiddleware.MARKER, html)
        self.assertEqual(html.count('"X-CSRFToken": "'), 1)
        # Токен в кэшированной странице — от второго посетителя, а не от первого.
        self.assertNotEqual(first.content, second.content)
        self.assertIn("CSRF_COOKIE", req2.META)

    def test_data_change_drops_cached_page(self):
        from core.live import bump_data_version

        self._get()
        bump_data_version()  # например, гол в live
        _, fresh = self._get()
        self.assertEqual(self.calls, 2)
        self.assertNotIn("X-Guest-Cache", fresh)

    def test_not_cached_for_users_toasts_and_other_paths(self):
        from users.models import User

        user = User(username="x")
        self._get(user=user)
        self._get(user=user)
        self._get(cookies={"messages": "x"})
        self._get("/users/profile/")
        self._get("/users/profile/")
        self.assertEqual(self.calls, 5)


class SessionRefreshTests(TestCase):
    def test_refresh_at_most_hourly(self):
        from unittest import mock

        from dopx.middleware import SessionRefreshMiddleware
        from users.models import User

        mw = SessionRefreshMiddleware(lambda r: HttpResponse("ok"))
        request = RequestFactory().get("/")
        request.user = User(username="x")
        request.session = SessionStore()
        with mock.patch("dopx.middleware.time.time", return_value=10_000):
            mw(request)
        self.assertTrue(request.session.modified)
        request.session.modified = False
        with mock.patch("dopx.middleware.time.time", return_value=10_000 + 600):
            mw(request)
        self.assertFalse(request.session.modified)  # через 10 минут — без записи
        with mock.patch("dopx.middleware.time.time", return_value=10_000 + 3700):
            mw(request)
        self.assertTrue(request.session.modified)


class TailwindAssetsTests(TestCase):
    def test_built_css_or_cdn(self):
        from core.templatetags.asset_extras import tailwind_assets

        with override_settings(TAILWIND_CDN=False):
            self.assertIn("css/app.css", tailwind_assets())
            self.assertNotIn("tailwindcss/browser", tailwind_assets())
        with override_settings(TAILWIND_CDN=True):
            self.assertIn("tailwindcss/browser", tailwind_assets())
