# round_squad/tests.py
"""
Тесты round_squad/services.py::_describe_notable_events_in_round ("Почему
он в сборной?" для тура, docs/adr/0030-rich-squad-explanation.md).
"""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

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


class RecomputeClosedRoundForceTests(TestCase):
    """2026-09-21, прямая просьба пользователя: "команда, которая
    перерасчёт делает всех закрытых туров сборные". Главные гарантии
    force=True (см. докстринг recompute_round): (1) реально пересчитывает
    уже закрытый тур на новых данных, (2) НЕ трогает finalized_at,
    (3) НЕ ставит повторную рассылку send_round_results_notification."""

    def setUp(self):
        self.league = League.objects.create(name="League", country="KZ")
        self.season = Season.objects.create(league=self.league, year="2026")
        self.home = Team.objects.create(name="Home")
        self.away = Team.objects.create(name="Away")
        self.match = Match.objects.create(
            league=self.league, season=self.season, home_team=self.home, away_team=self.away,
            start_time=timezone.now() - timedelta(days=1),
            voting_open_until=timezone.now() - timedelta(hours=1),  # тур уже закрыт
            status="finished", tour=7,
        )
        self.player = Player.objects.create(first_name="Игрок", last_name="Тестов", team=self.home)
        lineup = MatchLineup.objects.create(match=self.match, team=self.home, side="home")
        MatchLineupPlayer.objects.create(
            lineup=lineup, player=self.player, is_starting=True, shirt_number=9, position="ST",
        )
        self.aggregate = PlayerMatchAggregate.objects.create(
            player=self.player, match=self.match, performance_score=6.0, total_votes=10,
        )

    @patch("round_squad.tasks.send_round_results_notification.delay")
    def test_without_force_already_final_round_is_skipped(self, mock_delay):
        recompute_round(self.season, 7)  # первый вызов — тур закрывается
        round_xi = RoundBestXI.objects.get(season=self.season, tour=7)
        self.assertTrue(round_xi.is_final)
        first_computed_at = round_xi.last_computed_at
        mock_delay.assert_called_once()

        self.aggregate.performance_score = 9.9
        self.aggregate.save(update_fields=["performance_score"])

        recompute_round(self.season, 7)  # без force — должен молча пропустить

        round_xi.refresh_from_db()
        self.assertEqual(round_xi.last_computed_at, first_computed_at, "без force пересчёта быть не должно")
        mock_delay.assert_called_once()  # всё ещё ровно один вызов

    @patch("round_squad.tasks.send_round_results_notification.delay")
    def test_force_recomputes_without_resending_or_changing_finalized_at(self, mock_delay):
        recompute_round(self.season, 7)  # первый вызов — тур закрывается, письмо ставится в очередь
        round_xi = RoundBestXI.objects.get(season=self.season, tour=7)
        self.assertTrue(round_xi.is_final)
        original_finalized_at = round_xi.finalized_at
        self.assertIsNotNone(original_finalized_at)
        mock_delay.assert_called_once()

        # Правим данные задним числом — ровно тот сценарий из просьбы
        # пользователя ("данные матча поправили, а тур уже закрылся").
        self.aggregate.performance_score = 9.9
        self.aggregate.save(update_fields=["performance_score"])

        recompute_round(self.season, 7, force=True)

        round_xi.refresh_from_db()
        self.assertTrue(round_xi.is_final)
        self.assertEqual(
            round_xi.finalized_at, original_finalized_at,
            "force не должен сдвигать дату реальной фиксации тура",
        )
        self.assertEqual(
            round_xi.player_of_round_score, 9.9,
            "force ДОЛЖЕН пересчитать состав на новых данных",
        )
        # ГЛАВНАЯ ГАРАНТИЯ: письмо с итогами тура не улетело второй раз.
        mock_delay.assert_called_once()

    @patch("round_squad.tasks.send_round_results_notification.delay")
    def test_recompute_all_closed_rounds_processes_only_final_rounds(self, mock_delay):
        from round_squad.services import recompute_all_closed_rounds

        recompute_round(self.season, 7)  # закрывает тур 7
        mock_delay.assert_called_once()

        # Незакрытый тур в том же сезоне — не должен помешать/задеться.
        open_match = Match.objects.create(
            league=self.league, season=self.season, home_team=self.home, away_team=self.away,
            start_time=timezone.now() + timedelta(days=1),
            voting_open_until=timezone.now() + timedelta(days=3),
            status="scheduled", tour=8,
        )

        self.aggregate.performance_score = 3.3
        self.aggregate.save(update_fields=["performance_score"])

        processed = recompute_all_closed_rounds()

        self.assertEqual(processed, 1, "должен пересчитать ровно один закрытый тур (7), не трогая открытый (8)")
        self.assertFalse(RoundBestXI.objects.filter(season=self.season, tour=8).exists())
        round_xi = RoundBestXI.objects.get(season=self.season, tour=7)
        self.assertEqual(round_xi.player_of_round_score, 3.3)
        mock_delay.assert_called_once()  # по-прежнему один-единственный раз за весь тест
