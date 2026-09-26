# matches/tests_live_refresh.py
"""Фоновое обновление: когда блоки матча опрашивают сервер и что помечено как фоновое."""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from leagues.models import League
from lineups.models import MatchLineup, MatchLineupPlayer
from matches.models import Match
from players.models import Player
from seasons.models import Season
from teams.models import Team

User = get_user_model()


class LiveRefreshTests(TestCase):
    def setUp(self):
        league = League.objects.create(name="L", country="KZ")
        self.season = Season.objects.create(league=league, year="2026", is_active=True)
        self.home = Team.objects.create(name="H")
        self.away = Team.objects.create(name="A")
        self.league = league

    def make_match(self, status, start_in):
        start = timezone.now() + start_in
        return Match.objects.create(
            league=self.league, season=self.season, home_team=self.home, away_team=self.away,
            status=status, start_time=start, voting_open_until=start + timedelta(hours=48),
        )

    def test_poll_intervals(self):
        self.assertEqual(self.make_match("live", timedelta(minutes=-30)).live_poll_seconds, Match.LIVE_POLL_SECONDS)
        self.assertEqual(self.make_match("scheduled", timedelta(minutes=50)).live_poll_seconds, Match.PRE_MATCH_POLL_SECONDS)
        # Статус запаздывает — после времени старта опрос не выключается.
        self.assertEqual(self.make_match("scheduled", timedelta(minutes=-10)).live_poll_seconds, Match.PRE_MATCH_POLL_SECONDS)
        self.assertIsNone(self.make_match("scheduled", timedelta(days=2)).live_poll_seconds)
        self.assertIsNone(self.make_match("finished", timedelta(hours=-3)).live_poll_seconds)

    def test_pre_match_page_polls_header_and_lineups_in_background(self):
        match = self.make_match("scheduled", timedelta(minutes=50))
        html = self.client.get(reverse("matches:detail", args=[match.id])).content.decode()
        self.assertIn(reverse("matches:header", args=[match.id]), html)
        self.assertIn(reverse("matches:lineups", args=[match.id]), html)
        self.assertIn("dopx:wake", html)

    def test_lineups_partial_returns_published_lineups_and_stops_polling(self):
        match = self.make_match("scheduled", timedelta(minutes=50))
        lineup = MatchLineup.objects.create(match=match, team=self.home, side="home")
        player = Player.objects.create(first_name="Иван", last_name="Составной", team=self.home)
        MatchLineupPlayer.objects.create(lineup=lineup, player=player, is_starting=True, shirt_number=7)
        html = self.client.get(reverse("matches:lineups", args=[match.id])).content.decode()
        self.assertIn("Составной", html)
        self.assertNotIn("hx-trigger", html)

    def test_notification_badge_poll_is_background(self):
        user = User.objects.create_user(username="u", email="u@example.com", password="x", is_verified=True)
        self.client.force_login(user)
        html = self.client.get(reverse("notifications:unread_count_partial")).content.decode()
        self.assertIn("data-background-poll", html)
