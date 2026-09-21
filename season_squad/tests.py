# season_squad/tests.py
"""
Тесты season_squad/services.py::_describe_top_matches ("Почему он в
сборной?", docs/adr/0030-rich-squad-explanation.md).
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
from season_squad.services import (
    Candidate,
    _describe_nearest_competitor,
    _describe_top_matches,
    _player_season_position,
)
from seasons.models import Season
from teams.models import Team


class DescribeTopMatchesTests(TestCase):
    def setUp(self):
        self.league = League.objects.create(name="League", country="KZ")
        self.season = Season.objects.create(league=self.league, year="2026")
        self.team = Team.objects.create(name="Home")
        self.opponent1 = Team.objects.create(name="Opponent1")
        self.opponent2 = Team.objects.create(name="Opponent2")
        self.player = Player.objects.create(first_name="Иван", last_name="Иванов", team=self.team)

    def _make_match(self, opponent, days_ago, score):
        match = Match.objects.create(
            league=self.league, season=self.season,
            home_team=self.team, away_team=opponent,
            start_time=timezone.now() - timedelta(days=days_ago),
            voting_open_until=timezone.now() + timedelta(days=1),
            status="finished",
        )
        lineup = MatchLineup.objects.create(match=match, team=self.team, side="home")
        MatchLineupPlayer.objects.create(lineup=lineup, player=self.player, is_starting=True, shirt_number=10)
        PlayerMatchAggregate.objects.create(player=self.player, match=match, performance_score=score, total_votes=5)
        return match

    def test_no_matches_returns_empty(self):
        self.assertEqual(_describe_top_matches(str(self.player.id), self.season), "")

    def test_top_two_matches_by_score(self):
        self._make_match(self.opponent1, days_ago=10, score=6.0)
        self._make_match(self.opponent2, days_ago=5, score=9.5)
        self._make_match(self.opponent1, days_ago=1, score=8.0)
        text = _describe_top_matches(str(self.player.id), self.season)
        self.assertIn("9.5", text)
        self.assertIn("8.0", text)
        # БАГ ТЕСТА (найден пользователем, 2026-09-07): assertNotIn("6.0", text)
        # ложно падал — дата третьего матча форматируется как "06.09", а эта
        # строка САМА содержит подстроку "6.0" (символы '6','.','0' из "06.09"),
        # никак не связанную с исключённым счётом 6.0. Оценка всегда идёт в
        # формате "{score} — {date}", поэтому "6.0 —" однозначно ловит именно
        # счёт, а не случайное совпадение с датой.
        self.assertNotIn("6.0 —", text)
        self.assertIn("Opponent2", text)

    def test_notable_event_included(self):
        match = self._make_match(self.opponent1, days_ago=1, score=9.0)
        MatchEvent.objects.create(match=match, player=self.player, minute=78, event_type="goal", team_side="home")
        text = _describe_top_matches(str(self.player.id), self.season)
        self.assertIn("гол", text)
        self.assertIn("78", text)


def _candidate(name="Конкурент"):
    return Candidate(
        content_type_id=1, object_id="00000000-0000-0000-0000-000000000001",
        name=name, team_name="Team", photo_url="", profile_url="",
        raw_avg=0.0, matches=5, votes=20,
    )


class DescribeNearestCompetitorTests(SimpleTestCase):
    """"Сравнение с ближайшим конкурентом" (docs/adr/0032-squad-explainability-v2.md)
    — чистая функция над уже посчитанными числами, БД не нужна."""

    def test_no_runner_up_returns_empty(self):
        self.assertEqual(_describe_nearest_competitor(8.0, None), "")

    def test_positive_gap_returns_sentence(self):
        text = _describe_nearest_competitor(8.5, (_candidate("Иванов"), 7.9))
        self.assertIn("Иванов", text)
        self.assertIn("0.60", text)
        self.assertIn("7.90", text)

    def test_zero_or_negative_gap_returns_empty(self):
        """occupant слота по построению ранг №1 — нулевая/отрицательная
        разница означает эффект округления в _rank_pool, а не реальную
        ничью; вводящую в заблуждение фразу "обошёл на 0.00" не показываем."""
        self.assertEqual(_describe_nearest_competitor(8.0, (_candidate(), 8.0)), "")


class PlayerSeasonPositionTests(TestCase):
    """2026-09-21, прямая жалоба пользователя: "некоторые игроки стоят
    например на правом полузащитнике, а сам игрок например не играет там
    вообще". Одна из двух корневых причин (вторая — в players/positions.py::
    SLOT_PROCESSING_ORDER, см. round_squad/tests.py) — _player_season_
    position раньше считал моду позиции по ВСЕМ строкам MatchLineupPlayer,
    включая невышедших запасных, у которых нет вообще никакой информации
    о реальном амплуа на поле."""

    def setUp(self):
        self.league = League.objects.create(name="League", country="KZ")
        self.season = Season.objects.create(league=self.league, year="2026")
        self.team = Team.objects.create(name="Home")
        self.opponent = Team.objects.create(name="Away")
        self.player = Player.objects.create(first_name="Игрок", last_name="Тестов", team=self.team)

    def _make_match(self, days_ago):
        return Match.objects.create(
            league=self.league, season=self.season,
            home_team=self.team, away_team=self.opponent,
            start_time=timezone.now() - timedelta(days=days_ago),
            voting_open_until=timezone.now() + timedelta(days=1),
            status="finished",
        )

    def test_unused_bench_appearances_do_not_dominate_mode(self):
        """Игрок 5 раз был в заявке невышедшим запасным под общим "AM"
        (голое амплуа без стороны — так регистрируется скамейка, см.
        докстринг _player_season_position) и всего 1 раз реально вышел на
        поле — на левом фланге защиты (D:L). Сезонная позиция должна быть
        D:L, а не "AM" по большинству строк скамейки."""
        for i in range(5):
            match = self._make_match(days_ago=10 + i)
            lineup = MatchLineup.objects.create(match=match, team=self.team, side="home")
            MatchLineupPlayer.objects.create(
                lineup=lineup, player=self.player, is_starting=False, minute_in=None,
                position="AM", field_position="",
            )
        played_match = self._make_match(days_ago=1)
        lineup = MatchLineup.objects.create(match=played_match, team=self.team, side="home")
        MatchLineupPlayer.objects.create(
            lineup=lineup, player=self.player, is_starting=True,
            position="D", field_position="L",
        )

        result = _player_season_position(self.season)
        self.assertEqual(result[str(self.player.id)], "D:L")

    def test_substitute_appearance_counts_toward_mode(self):
        """Контрольная проверка: реальный выход на замену (minute_in
        задан) — это НЕ невышедшая скамейка, такая строка должна
        по-прежнему учитываться в моде."""
        match = self._make_match(days_ago=1)
        lineup = MatchLineup.objects.create(match=match, team=self.team, side="home")
        MatchLineupPlayer.objects.create(
            lineup=lineup, player=self.player, is_starting=False, minute_in=60,
            position="M", field_position="R",
        )

        result = _player_season_position(self.season)
        self.assertEqual(result[str(self.player.id)], "M:R")
        self.assertEqual(_describe_nearest_competitor(8.0, (_candidate(), 8.1)), "")
