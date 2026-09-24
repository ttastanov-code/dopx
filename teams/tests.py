# teams/tests.py
"""Тесты teams: индекс настроения клуба, график, состав команды, таблица перед матчем."""
from __future__ import annotations

from datetime import timedelta

from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from aggregates.models import RefereeMatchAggregate, TeamMatchAggregate
from leagues.models import League
from matches.models import Match
from players.models import Player
from referees.models import Referee
from seasons.models import Season
from teams.models import Team, TeamSeason
from teams.services import (
    build_mood_chart,
    build_sparkline_points,
    compute_mood_series,
    compute_mood_trend,
    find_season_controversial_matches,
    get_pre_match_standings_snapshot,
)

LOCMEM_CACHES = {
    "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "test-mood-trend"},
}


@override_settings(CACHES=LOCMEM_CACHES)
class MoodTrendServiceTests(TestCase):
    def setUp(self):
        cache.clear()
        self.league = League.objects.create(name="League", country="KZ")
        self.season = Season.objects.create(league=self.league, year="2026")
        self.team = Team.objects.create(name="Team")
        self.opponent = Team.objects.create(name="Opponent")

    def _make_match_agg(self, days_ago, score, total_votes=10):
        match = Match.objects.create(
            league=self.league, season=self.season,
            home_team=self.team, away_team=self.opponent,
            start_time=timezone.now() - timedelta(days=days_ago),
            voting_open_until=timezone.now() + timedelta(days=1),
            status="finished",
        )
        return TeamMatchAggregate.objects.create(
            team=self.team, match=match, total_votes=total_votes, performance_score=score,
        )

    def test_less_than_two_matches_returns_none(self):
        self._make_match_agg(days_ago=1, score=7.0)
        self.assertIsNone(compute_mood_trend(self.team))

    def test_rising_trend(self):
        self._make_match_agg(days_ago=5, score=5.0)
        self._make_match_agg(days_ago=4, score=5.0)
        self._make_match_agg(days_ago=3, score=5.0)
        self._make_match_agg(days_ago=2, score=8.0)
        self._make_match_agg(days_ago=1, score=8.0)
        result = compute_mood_trend(self.team)
        self.assertEqual(result["direction"], "up")

    def test_falling_trend(self):
        self._make_match_agg(days_ago=5, score=8.0)
        self._make_match_agg(days_ago=4, score=8.0)
        self._make_match_agg(days_ago=3, score=8.0)
        self._make_match_agg(days_ago=2, score=5.0)
        self._make_match_agg(days_ago=1, score=5.0)
        result = compute_mood_trend(self.team)
        self.assertEqual(result["direction"], "down")

    def test_flat_trend_within_noise_threshold(self):
        self._make_match_agg(days_ago=3, score=7.0)
        self._make_match_agg(days_ago=2, score=7.1)
        self._make_match_agg(days_ago=1, score=7.0)
        result = compute_mood_trend(self.team)
        self.assertEqual(result["direction"], "flat")

    def test_zero_vote_aggregates_excluded(self):
        self._make_match_agg(days_ago=2, score=9.0, total_votes=0)
        self._make_match_agg(days_ago=1, score=5.0)
        # Одна строка с голосами — мало для тренда.
        self.assertIsNone(compute_mood_trend(self.team))

    def test_result_is_cached(self):
        self._make_match_agg(days_ago=2, score=5.0)
        self._make_match_agg(days_ago=1, score=8.0)
        first = compute_mood_trend(self.team)
        # Проверяем кэш: новая строка не должна повлиять.
        self._make_match_agg(days_ago=0, score=1.0)
        second = compute_mood_trend(self.team)
        self.assertEqual(first, second)


@override_settings(CACHES=LOCMEM_CACHES)
class MoodSeriesServiceTests(TestCase):
    """Time series настроения клуба."""

    def setUp(self):
        cache.clear()
        self.league = League.objects.create(name="League", country="KZ")
        self.season = Season.objects.create(league=self.league, year="2026")
        self.team = Team.objects.create(name="Team")
        self.opponent = Team.objects.create(name="Opponent")

    def _make_match_agg(self, days_ago, mood_score, trust_score, total_votes=10):
        match = Match.objects.create(
            league=self.league, season=self.season,
            home_team=self.team, away_team=self.opponent,
            start_time=timezone.now() - timedelta(days=days_ago),
            voting_open_until=timezone.now() + timedelta(days=1),
            status="finished",
        )
        TeamMatchAggregate.objects.create(
            team=self.team, match=match, total_votes=total_votes,
            performance_score=mood_score, own_fans_avg=trust_score,
        )
        return match

    def test_no_matches_returns_empty_list(self):
        self.assertEqual(compute_mood_series(self.team), [])

    def test_points_chronological_with_trust_and_mood(self):
        self._make_match_agg(days_ago=3, mood_score=6.0, trust_score=7.0)
        self._make_match_agg(days_ago=1, mood_score=8.0, trust_score=9.0)
        series = compute_mood_series(self.team)
        self.assertEqual(len(series), 2)
        # Старые матчи первыми.
        self.assertEqual(series[0]["mood"], 6.0)
        self.assertEqual(series[1]["mood"], 8.0)
        self.assertEqual(series[0]["trust"], 7.0)

    def test_expectation_none_without_predictions(self):
        self._make_match_agg(days_ago=1, mood_score=7.0, trust_score=7.0)
        series = compute_mood_series(self.team)
        self.assertIsNone(series[0]["expectation_pct"])
        self.assertEqual(series[0]["total_predictions"], 0)

    def test_expectation_percentage_from_predictions(self):
        from predictions.models import MatchPrediction
        from django.contrib.auth import get_user_model

        User = get_user_model()
        match = self._make_match_agg(days_ago=1, mood_score=7.0, trust_score=7.0)
        for i in range(3):
            user = User.objects.create_user(username=f"u{i}", email=f"u{i}@example.com", password="pass12345")
            MatchPrediction.objects.create(match=match, user=user, choice=MatchPrediction.CHOICE_HOME)
        user_away = User.objects.create_user(username="u_away", email="u_away@example.com", password="pass12345")
        MatchPrediction.objects.create(match=match, user=user_away, choice=MatchPrediction.CHOICE_AWAY)

        series = compute_mood_series(self.team)
        # 3 из 4 прогнозов на победу хозяев => 75%.
        self.assertEqual(series[0]["expectation_pct"], 75.0)
        self.assertEqual(series[0]["total_predictions"], 4)


class BuildSparklinePointsTests(SimpleTestCase):
    def test_fewer_than_two_points_returns_empty(self):
        self.assertEqual(build_sparkline_points([{"v": 5.0}], "v"), "")
        self.assertEqual(build_sparkline_points([], "v"), "")

    def test_all_none_returns_empty(self):
        series = [{"v": None}, {"v": None}]
        self.assertEqual(build_sparkline_points(series, "v"), "")

    def test_two_valid_points_returns_coordinates(self):
        series = [{"v": 0.0}, {"v": 10.0}]
        points = build_sparkline_points(series, "v", value_max=10.0, width=100, height=100, pad_x=10, pad_y=10)
        self.assertNotEqual(points, "")
        coords = [tuple(map(float, p.split(","))) for p in points.split(" ")]
        self.assertEqual(len(coords), 2)
        # 0 — внизу, value_max — вверху.
        self.assertGreater(coords[0][1], coords[1][1])

    def test_gap_in_series_skipped_without_breaking_positions(self):
        series = [{"v": 5.0}, {"v": None}, {"v": 8.0}]
        points = build_sparkline_points(series, "v", value_max=10.0)
        coords = [tuple(map(float, p.split(","))) for p in points.split(" ")]
        self.assertEqual(len(coords), 2)  # точка с None пропущена


class BuildMoodChartTests(SimpleTestCase):
    """build_mood_chart: mood/trust — всегда dict с has_data и своей сеткой;
    expectation.bars включает матчи без прогнозов (has_data=False);
    latest_* — последнее реально известное значение.
    """

    def _point(self, label="01.09", opponent="Соперник", mood=6.0, trust=None, expectation_pct=None):
        return {"label": label, "opponent": opponent, "mood": mood, "trust": trust, "expectation_pct": expectation_pct}

    def test_empty_series_returns_none(self):
        self.assertIsNone(build_mood_chart([]))

    def test_missing_trust_data_has_data_false_but_dict_present(self):
        series = [self._point(mood=6.0), self._point(mood=7.0)]
        chart = build_mood_chart(series)
        self.assertTrue(chart["mood"]["has_data"])
        self.assertIsNotNone(chart["trust"])  # dict, не None
        self.assertFalse(chart["trust"]["has_data"])

    def test_single_trust_point_insufficient_for_line(self):
        # Одна точка trust — линию не строим.
        series = [self._point(mood=6.0, trust=5.0), self._point(mood=7.0, trust=None)]
        chart = build_mood_chart(series)
        self.assertFalse(chart["trust"]["has_data"])
        self.assertEqual(chart["latest_trust"], 5.0)  # число доступно

    def test_both_series_present_with_two_plus_points(self):
        series = [self._point(mood=6.0, trust=5.0), self._point(mood=7.0, trust=6.0)]
        chart = build_mood_chart(series)
        self.assertTrue(chart["mood"]["has_data"])
        self.assertTrue(chart["trust"]["has_data"])
        self.assertEqual(len(chart["mood"]["dots"]), 2)
        self.assertEqual(len(chart["trust"]["dots"]), 2)

    def test_mood_gridlines_are_whole_numbers_only(self):
        # Только 0/5/10.
        series = [self._point(mood=6.0), self._point(mood=7.0)]
        chart = build_mood_chart(series)
        labels = [g["label"] for g in chart["mood"]["gridlines"]]
        self.assertEqual(labels, ["0", "5", "10"])

    def test_trust_gridlines_are_minimal(self):
        series = [self._point(mood=6.0, trust=5.0), self._point(mood=7.0, trust=6.0)]
        chart = build_mood_chart(series)
        labels = [g["label"] for g in chart["trust"]["gridlines"]]
        self.assertEqual(labels, ["0", "10"])

    def test_expectation_bars_include_no_data_entries_not_excluded(self):
        # pct=None остаётся в bars с has_data=False.
        series = [self._point(mood=6.0, expectation_pct=40.0), self._point(mood=7.0, expectation_pct=None)]
        chart = build_mood_chart(series)
        bars = chart["expectation"]["bars"]
        self.assertEqual(len(bars), 2)
        self.assertTrue(bars[0]["has_data"])
        self.assertEqual(bars[0]["pct"], 40.0)
        self.assertFalse(bars[1]["has_data"])

    def test_has_expectation_data_false_when_all_none(self):
        series = [self._point(mood=6.0), self._point(mood=7.0)]
        chart = build_mood_chart(series)
        self.assertFalse(chart["has_expectation_data"])

    def test_has_expectation_data_true_even_with_single_data_point(self):
        # Секция ожиданий показывается при хотя бы одной точке.
        series = [self._point(mood=6.0, expectation_pct=50.0)] + [self._point(mood=6.0) for _ in range(9)]
        chart = build_mood_chart(series)
        self.assertTrue(chart["has_expectation_data"])
        bars = chart["expectation"]["bars"]
        self.assertTrue(bars[0]["has_data"])
        self.assertTrue(all(not b["has_data"] for b in bars[1:]))

    def test_latest_uses_last_non_null_value_not_series_tail(self):
        # latest_trust берёт более раннее известное значение.
        series = [
            self._point(label="01.09", opponent="A", mood=6.0, trust=5.0),
            self._point(label="08.09", opponent="B", mood=7.5, trust=None),
        ]
        chart = build_mood_chart(series)
        self.assertEqual(chart["latest_mood"], 7.5)
        self.assertEqual(chart["latest_trust"], 5.0)

    def test_latest_values_and_labels(self):
        series = [self._point(label="01.09", opponent="A", mood=6.0, trust=5.0),
                   self._point(label="08.09", opponent="B", mood=7.5, trust=6.5)]
        chart = build_mood_chart(series)
        self.assertEqual(chart["latest_mood"], 7.5)
        self.assertEqual(chart["latest_trust"], 6.5)
        self.assertEqual(chart["oldest_label"], "A 01.09")
        self.assertEqual(chart["newest_label"], "B 08.09")


@override_settings(CACHES=LOCMEM_CACHES)
class SeasonControversialMatchesTests(TestCase):
    def setUp(self):
        cache.clear()
        self.league = League.objects.create(name="League", country="KZ")
        self.season = Season.objects.create(league=self.league, year="2026")
        self.team = Team.objects.create(name="Team")
        self.opponent = Team.objects.create(name="Opponent")
        self.referee = Referee.objects.create(first_name="Судья", last_name="Судьич")

    def _make_match_with_gap(self, home_avg, away_avg):
        match = Match.objects.create(
            league=self.league, season=self.season,
            home_team=self.team, away_team=self.opponent,
            start_time=timezone.now() - timedelta(days=1),
            voting_open_until=timezone.now() + timedelta(days=1),
            status="finished", home_score=1, away_score=0,
        )
        RefereeMatchAggregate.objects.create(
            referee=self.referee, match=match, total_votes=10,
            home_fans_avg=home_avg, away_fans_avg=away_avg,
        )
        return match

    def test_small_gap_excluded(self):
        self._make_match_with_gap(7.0, 6.5)
        self.assertEqual(find_season_controversial_matches(self.team, self.season), [])

    def test_large_gap_included_and_sorted(self):
        self._make_match_with_gap(8.5, 4.0)  # gap 4.5
        self._make_match_with_gap(7.0, 5.0)  # gap 2.0
        result = find_season_controversial_matches(self.team, self.season)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["gap"], 4.5)
        self.assertEqual(result[1]["gap"], 2.0)

    def test_missing_segment_excluded(self):
        match = Match.objects.create(
            league=self.league, season=self.season,
            home_team=self.team, away_team=self.opponent,
            start_time=timezone.now() - timedelta(days=1),
            voting_open_until=timezone.now() + timedelta(days=1),
            status="finished",
        )
        RefereeMatchAggregate.objects.create(
            referee=self.referee, match=match, total_votes=10,
            home_fans_avg=8.0, away_fans_avg=None,
        )
        self.assertEqual(find_season_controversial_matches(self.team, self.season), [])


class TeamDetailViewRosterTests(TestCase):
    """Состав команды в активном сезоне (TeamDetailView)."""

    def setUp(self):
        self.league = League.objects.create(name="КПЛ", country="Казахстан", is_primary=True)
        self.season = Season.objects.create(league=self.league, year="2026", is_active=True)
        self.team = Team.objects.create(name="Кайрат")
        TeamSeason.objects.create(team=self.team, season=self.season)

    def _get_players(self):
        response = self.client.get(reverse('teams:detail', args=[self.team.id]))
        return list(response.context['players'])

    def test_player_who_only_played_past_season_excluded(self):
        """Игрок с историей, но не в активном сезоне — не в составе."""
        from lineups.models import MatchLineup, MatchLineupPlayer

        past_season = Season.objects.create(league=self.league, year="2025", is_active=False)
        arad = Player.objects.create(first_name="Офри", last_name="Арад", team=self.team)
        past_match = Match.objects.create(
            league=self.league, season=past_season,
            home_team=self.team, away_team=Team.objects.create(name="Соперник 2025"),
            status='finished', start_time=timezone.now() - timedelta(days=400),
            voting_open_until=timezone.now() - timedelta(days=397),
            home_score=1, away_score=0,
        )
        past_lineup = MatchLineup.objects.create(match=past_match, team=self.team, side='home')
        MatchLineupPlayer.objects.create(lineup=past_lineup, player=arad, is_starting=True)

        self.assertNotIn(arad, self._get_players())

    def test_new_signee_with_no_history_shown(self):
        """Новичок без матчей — в составе."""
        rookie = Player.objects.create(first_name="Новичок", last_name="БезМатчей", team=self.team)
        self.assertIn(rookie, self._get_players())

    def test_player_who_played_this_season_shown(self):
        """Сыграл в текущем сезоне — в составе."""
        from lineups.models import MatchLineup, MatchLineupPlayer

        played = Player.objects.create(first_name="Игрок", last_name="ТекущегоСезона", team=self.team)
        match = Match.objects.create(
            league=self.league, season=self.season,
            home_team=self.team, away_team=Team.objects.create(name="Соперник"),
            status='finished', start_time=timezone.now() - timedelta(days=10),
            voting_open_until=timezone.now() - timedelta(days=7),
            home_score=2, away_score=1,
        )
        lineup = MatchLineup.objects.create(match=match, team=self.team, side='home')
        MatchLineupPlayer.objects.create(lineup=lineup, player=played, is_starting=True)

        self.assertIn(played, self._get_players())

    def test_player_who_transferred_away_after_playing_this_season_still_shown(self):
        """Ушёл по ходу сезона — остаётся в составе прежнего клуба за этот сезон."""
        from lineups.models import MatchLineup, MatchLineupPlayer

        other_team = Team.objects.create(name="Новый клуб")
        transferred = Player.objects.create(
            first_name="Игрок", last_name="Трансферный", team=other_team,
        )
        match = Match.objects.create(
            league=self.league, season=self.season,
            home_team=self.team, away_team=Team.objects.create(name="Соперник"),
            status='finished', start_time=timezone.now() - timedelta(days=60),
            voting_open_until=timezone.now() - timedelta(days=57),
            home_score=1, away_score=1,
        )
        lineup = MatchLineup.objects.create(match=match, team=self.team, side='home')
        MatchLineupPlayer.objects.create(lineup=lineup, player=transferred, is_starting=True)

        self.assertIn(transferred, self._get_players())

    def test_large_squad_not_truncated_to_25(self):
        """Состав не режется до 25 игроков."""
        from lineups.models import MatchLineup, MatchLineupPlayer

        opponent = Team.objects.create(name="Соперник (большой состав)")
        match = Match.objects.create(
            league=self.league, season=self.season,
            home_team=self.team, away_team=opponent,
            status='finished', start_time=timezone.now() - timedelta(days=5),
            voting_open_until=timezone.now() - timedelta(days=2),
            home_score=3, away_score=0,
        )
        lineup = MatchLineup.objects.create(match=match, team=self.team, side='home')

        squad = []
        for number in range(1, 31):
            player = Player.objects.create(
                first_name="Игрок", last_name=f"Номер{number}", team=self.team, number=number,
            )
            MatchLineupPlayer.objects.create(lineup=lineup, player=player, is_starting=(number <= 11))
            squad.append(player)

        players = self._get_players()
        self.assertEqual(len(players), 30, "состав команды не должен обрезаться искусственным лимитом")
        last_player = squad[-1]  # номер 30
        self.assertIn(last_player, players)

    def test_large_squad_not_truncated_to_25_for_past_season_too(self):
        """То же для прошлого сезона."""
        from lineups.models import MatchLineup, MatchLineupPlayer

        past_season = Season.objects.create(league=self.league, year="2025", is_active=False)
        # Нужен TeamSeason, иначе ?season=2025 игнорируется.
        TeamSeason.objects.create(team=self.team, season=past_season)
        opponent = Team.objects.create(name="Соперник (прошлый сезон)")
        match = Match.objects.create(
            league=self.league, season=past_season,
            home_team=self.team, away_team=opponent,
            status='finished', start_time=timezone.now() - timedelta(days=400),
            voting_open_until=timezone.now() - timedelta(days=397),
            home_score=2, away_score=0,
        )
        lineup = MatchLineup.objects.create(match=match, team=self.team, side='home')

        squad = []
        for number in range(1, 31):
            player = Player.objects.create(
                first_name="Игрок25", last_name=f"Номер{number}", team=self.team, number=number,
            )
            MatchLineupPlayer.objects.create(lineup=lineup, player=player, is_starting=(number <= 11))
            squad.append(player)

        response = self.client.get(reverse('teams:detail', args=[self.team.id]), {'season': '2025'})
        players = list(response.context['players'])
        self.assertEqual(len(players), 30)
        self.assertIn(squad[-1], players)


class GetPreMatchStandingsSnapshotTests(TestCase):
    """Виджет «Турнирная таблица перед матчем»."""

    def setUp(self):
        self.league = League.objects.create(name="League", country="KZ")
        self.season = Season.objects.create(league=self.league, year="2026")
        self.team_a = Team.objects.create(name="Алатау")
        self.team_b = Team.objects.create(name="Женис")
        self.team_c = Team.objects.create(name="Третья команда")
        for team in (self.team_a, self.team_b, self.team_c):
            TeamSeason.objects.create(team=team, season=self.season)

    def test_none_when_no_matches_played_yet(self):
        """1-й тур — виджет не показываем."""
        upcoming = Match.objects.create(
            league=self.league, season=self.season,
            home_team=self.team_a, away_team=self.team_b,
            status='scheduled', start_time=timezone.now() + timedelta(days=1),
            voting_open_until=timezone.now() + timedelta(days=2),
        )
        self.assertIsNone(get_pre_match_standings_snapshot(upcoming))

    def test_snapshot_reflects_standings_right_before_this_match(self):
        # team_a — team_c 3:0.
        Match.objects.create(
            league=self.league, season=self.season,
            home_team=self.team_a, away_team=self.team_c,
            status='finished', start_time=timezone.now() - timedelta(days=10),
            voting_open_until=timezone.now() - timedelta(days=9),
            home_score=3, away_score=0,
        )
        # team_b — team_c 1:0.
        Match.objects.create(
            league=self.league, season=self.season,
            home_team=self.team_b, away_team=self.team_c,
            status='finished', start_time=timezone.now() - timedelta(days=8),
            voting_open_until=timezone.now() - timedelta(days=7),
            home_score=1, away_score=0,
        )
        upcoming = Match.objects.create(
            league=self.league, season=self.season,
            home_team=self.team_a, away_team=self.team_b,
            status='scheduled', start_time=timezone.now() + timedelta(days=1),
            voting_open_until=timezone.now() + timedelta(days=2),
        )
        # Матч после upcoming не влияет на снимок.
        Match.objects.create(
            league=self.league, season=self.season,
            home_team=self.team_c, away_team=self.team_b,
            status='finished', start_time=timezone.now() + timedelta(days=5),
            voting_open_until=timezone.now() + timedelta(days=6),
            home_score=0, away_score=0,
        )

        snapshot = get_pre_match_standings_snapshot(upcoming)

        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot['total_teams'], 3)
        self.assertEqual(snapshot['home']['team'], self.team_a)
        self.assertEqual(snapshot['home']['points'], 3)
        self.assertEqual(snapshot['home']['goal_diff'], 3)
        self.assertEqual(snapshot['home']['position'], 1)
        self.assertEqual(snapshot['away']['team'], self.team_b)
        self.assertEqual(snapshot['away']['points'], 3)
        self.assertEqual(snapshot['away']['goal_diff'], 1)
        self.assertEqual(snapshot['away']['position'], 2)
