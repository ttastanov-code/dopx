# core/tests_mourning.py
"""Режим траура: период, дашборд, плашка и монохром на сайте, реклама, push."""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import MourningMode
from core.mourning import current_mourning, push_allowed
from dashboard.models import AuditAction, StaffActionLog
from users.models import User


class MourningStateTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_period_bounds(self):
        now = timezone.now()
        MourningMode.objects.create(pk=1, is_enabled=True, starts_at=now + timedelta(hours=1))
        self.assertIsNone(current_mourning())
        MourningMode.objects.filter(pk=1).update(starts_at=now - timedelta(hours=1), ends_at=now + timedelta(hours=1))
        cache.clear()
        self.assertIsNotNone(current_mourning())
        MourningMode.objects.filter(pk=1).update(ends_at=now - timedelta(minutes=1))
        cache.clear()
        self.assertIsNone(current_mourning())

    def test_push_muted_except_essential(self):
        MourningMode.objects.create(pk=1, is_enabled=True, mute_push=True)
        self.assertFalse(push_allowed("match_event"))
        self.assertTrue(push_allowed("match_changed"))


@override_settings(STAFF_2FA_ENFORCED=False)
class MourningDashboardAndSiteTests(TestCase):
    def setUp(self):
        cache.clear()
        self.admin = User.objects.create_superuser(username="boss", email="boss@example.com", password="x")

    def test_enable_shows_banner_grayscale_and_logs(self):
        self.client.force_login(self.admin)
        url = reverse("dashboard:mourning")
        self.assertEqual(self.client.get(url).status_code, 200)
        self.client.post(url, {"action": "save", "is_enabled": "on", "message": "Траур", "grayscale": "on", "hide_ads": "on"})
        self.assertTrue(StaffActionLog.objects.filter(action=AuditAction.MOURNING_CHANGED).exists())
        self.client.logout()
        html = self.client.get(reverse("core:home")).content.decode()
        self.assertIn("data-mourning", html)
        self.assertIn('id="mourning-banner"', html)
        self.assertIn("Траур", html)

        self.client.force_login(self.admin)
        self.client.post(url, {"action": "disable"})
        self.client.logout()
        self.assertNotIn('id="mourning-banner"', self.client.get(reverse("core:home")).content.decode())

    def test_ads_hidden(self):
        from django.template import Context, Template
        MourningMode.objects.create(pk=1, is_enabled=True, hide_ads=True)
        with patch("partners.templatetags.partner_tags.get_active_banner_for_zone") as get_banner:
            Template('{% load partner_tags %}{% render_banner "sidebar" %}').render(Context({"mourning": current_mourning()}))
        get_banner.assert_not_called()
