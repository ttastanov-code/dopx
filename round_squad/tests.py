# round_squad/tests.py
"""
Тесты round_squad/services.py::_describe_notable_events_in_round ("Почему
он в сборной?" для тура, docs/adr/0030-rich-squad-explanation.md).
"""
from __future__ import annotations

from datetime import timedelta

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from aggregates.models import PlayerMatchAggregate
from events.models import MatchEvent
from leagues.models import League
from lineups.models import MatchLineup, MatchLineupPlayer
from matches.models import Match
from players.models import Player
from round_squad.models import RoundBestXI, RoundBestXISlot
from round_squad.services import (
    RoundCandidate,
    _describe_nearest_competitor_round,
    _describe_notable_events_in_round,
    _describe_round_rank_change,
    recompute_round,
)
from seasons.models import Season
from teams.models import Team


class DescribeNotableEventsInRoundTests(TestCase):
    def setUp(self):
        self.league = League.objects.create(name="League", country="KZ")
        self.season = Season.objects.create(league=self.league, year="2026")
        self.home = Team.objects.create(name="Home")
        self.away = Team.objects.create(name="Away")
        self.player = Player.objects.create(first_name="Иван", last_name="Иванов", team=self.home)
        self.match = Match.objects.create(
            league=self.league, season=self.season, home_team=self.home, away_team=self.away,
            start_time=timezone.now() - timedelta(days=1),
            voting_open_until=timezone.now() + timedelta(days=1),
            status="finished", tour=5,
        )
        PlayerMatchAggregate.objects.create(
            player=self.player, match=self.match, performance_score=8.5, total_votes=6
        )

    def test_no_events_returns_empty(self):
        self.assertEqual(_describe_notable_events_in_round(str(self.player.id), self.season, 5), "")

    def test_no_aggregate_for_tour_returns_empty(self):
        self.assertEqual(_describe_notable_events_in_round(str(self.player.id), self.season, 99), "")

    def test_notable_events_described(self):
        MatchEvent.objects.create(
            match=self.match, player=self.player, minute=12, event_type="yellow_card", team_side="home"
        )
        MatchEvent.objects.create(
            match=self.match, player=self.player, minute=90, event_type="goal", team_side="home"
        )
        text = _describe_notable_events_in_round(str(self.player.id), self.season, 5)
        self.assertIn("Отличился", text)
        self.assertIn("жёлтая карточка", text)
        self.assertIn("гол", text)


def _round_candidate(name="Конкурент"):
    return RoundCandidate(
        content_type_id=1, object_id="00000000-0000-0000-0000-000000000002",
        name=name, team_name="Team", photo_url="", profile_url="",
        raw_avg=0.0, votes=10,
    )


class DescribeNearestCompetitorRoundTests(SimpleTestCase):
    """docs/adr/0032-squad-explainability-v2.md — тот же принцип, что
    season_squad._describe_nearest_competitor, для тура."""

    def test_no_runner_up_returns_empty(self):
        self.assertEqual(_describe_nearest_competitor_round(8.0, None), "")

    def test_positive_gap_returns_sentence(self):
        text = _describe_nearest_competitor_round(8.5, (_round_candidate("Петров"), 7.0))
        self.assertIn("Петров", text)
        self.assertIn("1.50", text)

    def test_non_positive_gap_returns_empty(self):
        self.assertEqual(_describe_nearest_competitor_round(8.0, (_round_candidate(), 8.0)), "")


class DescribeRoundRankChangeTests(SimpleTestCase):
    def test_new_returns_empty(self):
        """'new' сознательно НЕ показывается отдельной фразой — "не играл в
        прошлом туре" не всегда значимый факт (см. докстринг функции)."""
        self.assertEqual(_describe_round_rank_change(RoundBestXISlot.RANK_CHANGE_NEW, None), "")

    def test_same_returns_empty(self):
        self.assertEqual(_describe_round_rank_change(RoundBestXISlot.RANK_CHANGE_SAME, None), "")

    def test_up_returns_sentence(self):
        text = _describe_round_rank_change(RoundBestXISlot.RANK_CHANGE_UP, 2)
        self.assertIn("2 места", text)
        self.assertIn("прошлым туром", text)

    def test_up_single_place_uses_singular(self):
        text = _describe_round_rank_change(RoundBestXISlot.RANK_CHANGE_UP, 1)
        self.assertIn("1 место", text)


class RoundRankChangeAcrossToursTests(TestCase):
    """Интеграционный тест: recompute_round должен сравнивать с прошлым
    ЗАФИКСИРОВАННЫМ туром (не прошлым прогоном этого же тура), см.
    докстринг RoundBestXISlot.RANK_CHANGE_CHOICES."""

    def setUp(self):
        self.league = League.objects.create(name="League", country="KZ")
        self.season = Season.objects.create(league=self.league, year="2026")
        self.home = Team.objects.create(name="Home")
        self.away = Team.objects.create(name="Away")

    def _make_tour_match(self, tour):
        return Match.objects.create(
            league=self.league, season=self.season, home_team=self.home, away_team=self.away,
            start_time=timezone.now() - timedelta(days=1),
            voting_open_until=timezone.now() - timedelta(hours=1),  # тур уже закрыт
            status="finished", tour=tour,
        )

    def _make_starting_lineup_player(self, match, player, shirt_number):
        lineup = MatchLineup.objects.create(match=match, team=self.home, side="home")
        MatchLineupPlayer.objects.create(
            lineup=lineup, player=player, is_starting=True, shirt_number=shirt_number,
            position="GK",
        )

    def test_same_top_scorer_two_tours_in_a_row_is_same(self):
        player = Player.objects.create(first_name="Иван", last_name="Иванов", team=self.home)

        match1 = self._make_tour_match(tour=1)
        self._make_starting_lineup_player(match1, player, shirt_number=1)
        PlayerMatchAggregate.objects.create(player=player, match=match1, performance_score=8.0, total_votes=10)
        recompute_round(self.season, 1)

        slot1 = RoundBestXISlot.objects.get(round_best_xi__season=self.season, round_best_xi__tour=1, slot_code="GK")
        self.assertEqual(slot1.rank_change, RoundBestXISlot.RANK_CHANGE_NEW)

        match2 = self._make_tour_match(tour=2)
        self._make_starting_lineup_player(match2, player, shirt_number=1)
        PlayerMatchAggregate.objects.create(player=player, match=match2, performance_score=9.0, total_votes=10)
        recompute_round(self.season, 2)

        slot2 = RoundBestXISlot.objects.get(round_best_xi__season=self.season, round_best_xi__tour=2, slot_code="GK")
        self.assertEqual(slot2.rank_change, RoundBestXISlot.RANK_CHANGE_SAME)
