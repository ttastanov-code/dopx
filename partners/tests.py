# partners/tests.py
"""
Тесты B2B v1 (docs/adr/0034-club-mood-index-v2.md) —
partners/views.py::PartnerMoodIndexFeedView и
partners/services.py::build_mood_index_feed. Тот же токен-доступ, что
PartnerContentFeedView (Partner.feed_token, не отдельная модель авторизации).
"""
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
        """B2B-фид должен быть анонимизированным агрегатом — ни одного
        поля вида user/username/email/ip нигде в структуре ответа."""
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
