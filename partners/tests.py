# partners/tests.py
"""Тесты фида индекса настроения для партнёров."""
from __future__ import annotations

import json
from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from leagues.models import League
from matches.models import Match
from partners.models import Partner, PartnerType
from partners.services import build_mood_index_feed
from seasons.models import Season
from teams.models import Team


class PartnerMoodIndexFeedViewTests(TestCase):
    def setUp(self):
        self.league = League.objects.create(name="League", country="KZ")
        self.season = Season.objects.create(league=self.league, year="2026", is_active=True)
        self.team = Team.objects.create(name="Team")
        self.partner = Partner.objects.create(
            name="Media Partner", slug="media-partner", partner_type=PartnerType.MEDIA,
        )

    def _url(self, token=None):
        return reverse(
            "partners:mood_index_feed",
            args=["media-partner", token or self.partner.feed_token, self.team.id],
        )

    def test_wrong_token_returns_404(self):
        import uuid

        response = self.client.get(self._url(token=uuid.uuid4()))
        self.assertEqual(response.status_code, 404)

    def test_inactive_partner_returns_404(self):
        self.partner.is_active = False
        self.partner.save(update_fields=["is_active"])
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 404)

    def test_correct_token_returns_feed(self):
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["team"], "Team")
        self.assertIn("mood_series", data)
        self.assertIn("controversial_matches", data)
        self.assertEqual(response["Cache-Control"], "no-store")

    def test_feed_contains_no_user_level_fields(self):
        """В ответе нет данных пользователей."""
        response = self.client.get(self._url())
        raw = response.content.decode()
        for forbidden in ("username", "\"email\"", "\"user\"", "ip_address"):
            self.assertNotIn(forbidden, raw)


class BuildMoodIndexFeedServiceTests(TestCase):
    def setUp(self):
        self.league = League.objects.create(name="League", country="KZ")
        self.season = Season.objects.create(league=self.league, year="2026")
        self.team = Team.objects.create(name="Team")
        self.opponent = Team.objects.create(name="Opponent")
        self.partner = Partner.objects.create(
            name="Media Partner", slug="media-partner-2", partner_type=PartnerType.MEDIA,
        )

    def test_empty_data_returns_valid_empty_structure(self):
        data = build_mood_index_feed(self.partner, self.team, self.season)
        self.assertEqual(data["team"], "Team")
        self.assertEqual(data["season"], "2026")
        self.assertEqual(data["mood_series"], [])
        self.assertEqual(data["controversial_matches"], [])

    def test_none_season_handled(self):
        data = build_mood_index_feed(self.partner, self.team, None)
        self.assertIsNone(data["season"])
        self.assertEqual(data["controversial_matches"], [])


class BannerServingTests(TestCase):
    """Реклама: выбор баннера, лимиты, учёт показов по видимости, управление в дашборде."""

    def setUp(self):
        from django.core.cache import cache
        from django.core.files.uploadedfile import SimpleUploadedFile

        cache.clear()
        self.partner = Partner.objects.create(name="Brand", slug="brand", partner_type=PartnerType.OTHER)
        self.image = SimpleUploadedFile("b.png", _png(600, 500), content_type="image/png")

    def _banner(self, **extra):
        from partners.models import Banner

        defaults = dict(zone="sidebar", format="native", title="Native", headline="Hello",
                        target_url="https://example.com", partner=self.partner)
        defaults.update(extra)
        return Banner.objects.create(**defaults)

    def _request(self, user=None):
        from django.contrib.auth.models import AnonymousUser
        from django.contrib.sessions.backends.cache import SessionStore
        from django.test import RequestFactory

        request = RequestFactory().get("/")
        request.user = user or AnonymousUser()
        request.session = SessionStore()
        return request

    def test_pick_respects_schedule_audience_and_limits(self):
        from partners.services import pick_banner, record_impression

        self.assertIsNone(pick_banner("sidebar", self._request()))
        future = self._banner(starts_at=timezone.now() + timedelta(days=1))
        users_only = self._banner(audience="users")
        self.assertIsNone(pick_banner("sidebar", self._request()))
        self.assertEqual(future.status(), "scheduled")

        capped = self._banner(max_impressions=1, daily_cap_per_visitor=1)
        request = self._request()
        self.assertEqual(pick_banner("sidebar", request), capped)
        record_impression(capped, request)
        self.assertIsNone(pick_banner("sidebar", request))
        self.assertEqual(users_only.status(), "running")

    def test_draft_without_material_is_not_shown(self):
        from partners.services import pick_banner

        self._banner(format="image")
        self.assertIsNone(pick_banner("sidebar", self._request()))

    def test_view_beacon_and_click_count(self):
        from partners.selectors import banner_stats

        banner = self._banner()
        self.assertEqual(self.client.post(reverse("partners:banner_view", args=[banner.pk])).status_code, 204)
        response = self.client.get(reverse("partners:banner_click", args=[banner.pk]))
        self.assertIn("utm_source=dopx", response["Location"])
        self.assertEqual(banner_stats(banner.pk), {"impressions": 1, "clicks": 1, "ctr_percent": 100.0})

    def test_render_does_not_count_and_is_live_safe(self):
        from django.template import Context, Template

        banner = self._banner()
        html = Template('{% load partner_tags %}{% render_banner "sidebar" %}').render(Context({"request": self._request()}))
        self.assertIn("data-live-ignore", html)
        self.assertIn(reverse("partners:banner_view", args=[banner.pk]), html)
        self.assertIn("Реклама · Brand", html)
        self.assertFalse(banner.daily_stats.exists())

    def test_form_requires_material_per_format(self):
        from partners.forms import BannerForm

        form = BannerForm(data={"title": "x", "zone": "sidebar", "format": "native", "target_url": "https://e.com",
                                "priority": 1, "audience": "all", "daily_cap_per_visitor": 0, "cta_label": "Go"})
        self.assertFalse(form.is_valid())
        self.assertIn("headline", form.errors)
        form = BannerForm(data={"title": "x", "zone": "leaderboard", "format": "image", "target_url": "https://e.com",
                                "priority": 1, "audience": "all", "daily_cap_per_visitor": 0},
                          files={"image": self.image})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertTrue(any("обрежутся" in w for w in form.warnings))

    def test_dashboard_pages_and_report(self):
        from users.models import User

        banner = self._banner()
        admin = User.objects.create_superuser(username="boss", email="boss@test.local", password="x")
        self.client.force_login(admin)
        with self.settings(STAFF_2FA_ENFORCED=False):
            for name, args in (("banners_list", []), ("banner_create", []), ("banner_detail", [banner.pk]), ("banner_sandbox", [])):
                self.assertEqual(self.client.get(reverse(f"dashboard:{name}", args=args)).status_code, 200, name)
            frame = self.client.get(reverse("dashboard:banner_sandbox_frame") + "?banner=demo-image&zone=home_hero")
            self.assertContains(frame, "1200 × 300")
            self.client.post(reverse("dashboard:banner_toggle", args=[banner.pk]))
            banner.refresh_from_db()
            self.assertFalse(banner.is_active)
            self.client.post(reverse("dashboard:banner_duplicate", args=[banner.pk]))
            self.assertEqual(self.partner.banners.count(), 2)
        self.client.logout()
        report = reverse("partners:report", args=[self.partner.slug, self.partner.feed_token])
        self.assertContains(self.client.get(report), "Brand")
        bad = reverse("partners:report", args=[self.partner.slug, "00000000-0000-0000-0000-000000000000"])
        self.assertEqual(self.client.get(bad).status_code, 404)


def _png(width: int, height: int) -> bytes:
    from io import BytesIO

    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (width, height), (80, 70, 200)).save(buf, format="PNG")
    return buf.getvalue()


class WidgetEmbedTrackingTests(TestCase):
    """Встраиванием считается только загрузка виджета с чужого сайта."""

    def test_own_pages_and_direct_opens_not_counted(self):
        from analytics.models import AnalyticsEvent, EventName

        team = Team.objects.create(name="Кайрат")
        url = reverse("teams:widget", args=[team.id])
        self.client.get(url)
        self.client.get(url, HTTP_REFERER="http://testserver/teams/")
        self.client.get(url, HTTP_REFERER="http://localhost:8000/staff/dashboard/ads/")
        self.assertFalse(AnalyticsEvent.objects.filter(event_name=EventName.WIDGET_EMBED_VIEWED).exists())
        self.client.get(url, HTTP_REFERER="https://sports.kz/news/1")
        self.assertEqual(AnalyticsEvent.objects.filter(event_name=EventName.WIDGET_EMBED_VIEWED).count(), 1)


class BannerLiveStatsTests(TestCase):
    def test_stats_partial_and_version(self):
        from partners.models import Banner
        from users.models import User

        banner = Banner.objects.create(zone="sidebar", format="native", title="N", headline="H", target_url="https://e.com")
        self.client.force_login(User.objects.create_superuser(username="boss", email="b@t.local", password="x"))
        self.client.post(reverse("partners:banner_view", args=[banner.pk]))
        with self.settings(STAFF_2FA_ENFORCED=False):
            self.assertContains(self.client.get(reverse("dashboard:banner_stats_partial", args=[banner.pk])), "показов за 30 дней")
            self.assertContains(self.client.get(reverse("dashboard:banners_list") + "?partial=1"), 'CTR')
            self.assertIn("version", self.client.get(reverse("dashboard:banner_version", args=[banner.pk])).json())
