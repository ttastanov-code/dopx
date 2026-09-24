# matches/tests_stat_ratings.py
"""Тесты оценки «по статистике» и сохранения полного raw при импорте."""
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from leagues.models import League
from matches.models import Match, MatchPlayerStatistics, MatchTeamStatistics
from matches.stat_ratings import average_stat_rating, stat_ratings_for_match, stat_ratings_for_player
from parsers.sportmonks.importers import import_player_statistics, import_statistics
from players.models import Player
from seasons.models import Season
from teams.models import Team


class _Fixture(TestCase):
    def setUp(self):
        self.league = League.objects.create(name="Test League", country="KZ")
        self.season = Season.objects.create(league=self.league, year="2026")
        self.home = Team.objects.create(name="Home", sportmonks_id="100")
        self.away = Team.objects.create(name="Away", sportmonks_id="200")
        self.player = Player.objects.create(first_name="Иван", last_name="Тестов", team=self.home, sportmonks_id="555")

    def make_match(self):
        return Match.objects.create(
            league=self.league, season=self.season, home_team=self.home, away_team=self.away,
            status="finished", start_time=timezone.now() - timedelta(days=1),
            voting_open_until=timezone.now() + timedelta(hours=48),
        )


class StatRatingTests(_Fixture):
    def test_ratings_read_and_bad_values_ignored(self):
        m1, m2, m3 = self.make_match(), self.make_match(), self.make_match()
        other = Player.objects.create(first_name="П", last_name="Д", team=self.home)
        MatchPlayerStatistics.objects.create(match=m1, player=self.player, team=self.home, raw={"RATING": 7.0})
        MatchPlayerStatistics.objects.create(match=m2, player=self.player, team=self.home, raw={"RATING": "6.0"})
        MatchPlayerStatistics.objects.create(match=m3, player=self.player, team=self.home, raw={"RATING": 0})
        MatchPlayerStatistics.objects.create(match=m1, player=other, team=self.home, raw={"FOULS": 2})

        self.assertEqual(stat_ratings_for_match(m1), {self.player.id: 7.0})
        self.assertEqual(stat_ratings_for_player(self.player), {m1.id: 7.0, m2.id: 6.0})
        self.assertEqual(average_stat_rating(self.player), (6.5, 2))
        self.assertEqual(average_stat_rating(other), (None, 0))


class FullRawImportTests(_Fixture):
    def _detail(self, name, value):
        return {"type": {"developer_name": name}, "data": {"value": value}}

    def test_player_raw_keeps_all_types(self):
        """Регрессия: раньше в raw попадали только разобранные поля, а
        отборы/перехваты/оценка выбрасывались."""
        match = self.make_match()
        import_player_statistics(match, [{
            "player_id": 555, "team_id": 100,
            "details": [self._detail("FOULS", 2), self._detail("TACKLES", 3), self._detail("RATING", 6.9)],
        }])
        stats = MatchPlayerStatistics.objects.get(match=match, player=self.player)
        self.assertEqual(stats.fouls, 2)
        self.assertEqual(stats.raw["TACKLES"], 3)
        self.assertEqual(stats.raw["RATING"], 6.9)

    def test_team_raw_keeps_all_types(self):
        match = self.make_match()
        import_statistics(match, [
            {"participant_id": 100, **self._detail("CORNERS", 5)},
            {"participant_id": 100, **self._detail("INTERCEPTIONS", 17)},
        ])
        stats = MatchTeamStatistics.objects.get(match=match, team=self.home)
        self.assertEqual(stats.corners, 5)
        self.assertEqual(stats.raw["INTERCEPTIONS"], 17)
