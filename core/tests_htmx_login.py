# core/tests_htmx_login.py
"""Фоновые запросы без входа и next после входа: не вставлять страницу логина во фрагмент, не уводить на служебный адрес."""
from django.test import SimpleTestCase, TestCase

from core.utils import is_page_next


class PageNextTests(SimpleTestCase):
    def test_service_paths_rejected(self):
        self.assertFalse(is_page_next("/notifications/unread-count/"))
        self.assertFalse(is_page_next("/matches/abc/partial/"))
        self.assertFalse(is_page_next(""))
        self.assertTrue(is_page_next("/matches/abc/"))


class HtmxWithoutLoginTests(TestCase):
    def test_bell_for_guest_is_empty(self):
        r = self.client.get("/notifications/unread-count/")
        self.assertEqual((r.status_code, r.content), (200, b""))

    def test_background_get_gets_no_login_page(self):
        r = self.client.get("/notifications/", HTTP_HX_REQUEST="true")
        self.assertEqual(r.status_code, 204)
        self.assertNotIn("HX-Redirect", r)

    def test_user_action_redirects_to_login_back_to_page(self):
        r = self.client.post("/notifications/read-all/", HTTP_HX_REQUEST="true",
                             HTTP_HX_CURRENT_URL="http://testserver/matches/")
        self.assertEqual(r.status_code, 204)
        self.assertEqual(r["HX-Redirect"], "/users/login/?next=%2Fmatches%2F")

    def test_login_ignores_service_next(self):
        from django.test import RequestFactory
        from django.urls import reverse

        from users.views import LoginView

        view = LoginView()
        view.request = RequestFactory().get("/users/login/?next=/notifications/unread-count/")
        self.assertEqual(str(view.get_success_url()), reverse("core:home"))
        view.request = RequestFactory().get("/users/login/?next=/matches/")
        self.assertEqual(view.get_success_url(), "/matches/")
