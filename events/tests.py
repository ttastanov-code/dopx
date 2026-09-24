# events/tests.py
"""Тесты имён в событиях: если Player не найден, берём имя из extra_data."""
from __future__ import annotations

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from events.models import MatchEvent
from leagues.models import League
from matches.models import Match
from players.models import Player
from seasons.models import Season
from teams.models import Team


class MatchEventDisplayNameFallbackTests(TestCase):
    def setUp(self):
        self.league = League.objects.create(name="League", country="KZ")
        self.season = Season.objects.create(league=self.league, year="2026")
        self.home = Team.objects.create(name="Дома")
        self.away = Team.objects.create(name="Гости")
        self.match = Match.objects.create(
            league=self.league, season=self.season,
            home_team=self.home, away_team=self.away,
            status="live", start_time=timezone.now() - timedelta(minutes=30),
            voting_open_until=timezone.now() + timedelta(days=2),
        )

    def test_player_display_name_uses_real_player_when_matched(self):
        player = Player.objects.create(first_name="Иван", last_name="Иванов", team=self.home)
        event = MatchEvent.objects.create(
            match=self.match, minute=45, event_type="goal", team_side="home",
            player=player, extra_data={"player_name": "Должно быть проигнорировано"},
        )
        self.assertEqual(event.player_display_name, "Иван Иванов")

    def test_player_display_name_falls_back_to_raw_extra_data_when_player_missing(self):
        """player=None, имя из extra_data."""
        event = MatchEvent.objects.create(
            match=self.match, minute=45, event_type="goal", team_side="home",
            player=None, extra_data={"player_name": "Новый Легионер"},
        )
        self.assertEqual(event.player_display_name, "Новый Легионер")

    def test_player_display_name_none_when_nothing_available(self):
        event = MatchEvent.objects.create(
            match=self.match, minute=45, event_type="goal", team_side="home",
            player=None, extra_data={},
        )
        self.assertIsNone(event.player_display_name)

    def test_assist_display_name_falls_back_to_related_player_name(self):
        event = MatchEvent.objects.create(
            match=self.match, minute=50, event_type="goal", team_side="home",
            player=None, assist_player=None,
            extra_data={"player_name": "Забивака", "related_player_name": "Ассистент"},
        )
        self.assertEqual(event.player_display_name, "Забивака")
        self.assertEqual(event.assist_display_name, "Ассистент")

    def test_player_out_display_name_falls_back_for_substitutions(self):
        """Для замен related_player_name — ушедший игрок."""
        event = MatchEvent.objects.create(
            match=self.match, minute=60, event_type="substitution", team_side="away",
            player=None, player_out=None,
            extra_data={"player_name": "Вышел на замену", "related_player_name": "Ушёл с поля"},
        )
        self.assertEqual(event.player_display_name, "Вышел на замену")
        self.assertEqual(event.player_out_display_name, "Ушёл с поля")
