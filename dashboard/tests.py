# dashboard/tests.py
"""
Тесты агрегирующего слоя staff-дашборда (`dashboard/services.py`). Основной
риск здесь — тихое расхождение между "точным счётчиком" на карточке
(`.count()`) и обрезанным списком под ней (`[:20]`) при доработке дашборда
"как найти конкретный проблемный матч" (см. docstring
`data_health_summary`): если разработчик в будущем случайно заменит
раздельные `.count()`/`[:20]` на `len()` от одного и того же обрезанного
списка, цифра на карточке начнёт молча занижаться при >20 проблемных
матчах. DataHealthCountVsListTests закрывает именно это.
"""
from __future__ import annotations

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from dashboard.services import data_health_summary
from events.models import MatchEvent
from leagues.models import League
from lineups.models import MatchLineup
from matches.models import Match
from parsers.models import ParserSyncRun
from seasons.models import Season
from teams.models import Team


class DataHealthFixtureMixin:
    def setUp(self):
        self.league = League.objects.create(name="Test League", country="KZ")
        self.season = Season.objects.create(league=self.league, year="2026")
        self.home = Team.objects.create(name="Home")
        self.away = Team.objects.create(name="Away")

    def _make_match(self, *, status="finished", has_lineup=False, minutes_ago=0):
        return Match.objects.create(
            league=self.league, season=self.season, home_team=self.home, away_team=self.away,
            status=status, has_lineup=has_lineup,
            start_time=timezone.now() - timedelta(minutes=minutes_ago),
            voting_open_until=timezone.now() + timedelta(hours=48),
        )


class MissingLineupsDetectionTests(DataHealthFixtureMixin, TestCase):
    def test_finished_match_with_declared_lineup_but_no_rows_is_flagged(self):
        match = self._make_match(status="finished", has_lineup=True)
        health = data_health_summary()
        self.assertEqual(health["matches_missing_lineups"], 1)
        self.assertIn(match, health["matches_missing_lineups_list"])

    def test_match_with_actual_lineup_row_not_flagged(self):
        match = self._make_match(status="finished", has_lineup=True)
        MatchLineup.objects.create(match=match, team=self.home, side="home", formation="4-3-3")
        health = data_health_summary()
        self.assertEqual(health["matches_missing_lineups"], 0)
        self.assertNotIn(match, health["matches_missing_lineups_list"])

    def test_scheduled_match_never_flagged_even_without_lineup(self):
        """has_lineup=True на scheduled-матче не бывает в реальных данных
        (KFF выставляет флаг только когда состав реально опубликован), но
        фильтр всё равно должен требовать live/finished явно — это защита
        от будущих аномальных данных, а не проверка текущего инварианта."""
        self._make_match(status="scheduled", has_lineup=True)
        health = data_health_summary()
        self.assertEqual(health["matches_missing_lineups"], 0)

    def test_match_without_declared_lineup_not_flagged(self):
        """has_lineup=False — KFF ещё не опубликовал состав, это не ошибка
        синка, это нормальное состояние матча."""
        self._make_match(status="finished", has_lineup=False)
        health = data_health_summary()
        self.assertEqual(health["matches_missing_lineups"], 0)


class MissingEventsDetectionTests(DataHealthFixtureMixin, TestCase):
    def test_finished_match_without_events_is_flagged(self):
        match = self._make_match(status="finished")
        health = data_health_summary()
        self.assertEqual(health["matches_missing_events"], 1)
        self.assertIn(match, health["matches_missing_events_list"])

    def test_match_with_at_least_one_event_not_flagged(self):
        match = self._make_match(status="finished")
        MatchEvent.objects.create(match=match, minute=10, event_type="goal", team_side="home")
        health = data_health_summary()
        self.assertEqual(health["matches_missing_events"], 0)
        self.assertNotIn(match, health["matches_missing_events_list"])

    def test_live_match_without_events_is_also_flagged(self):
        """live, а не только finished — матч уже идёт, событий пока нет,
        это тоже сигнал проблемы синка, не только для завершённых."""
        self._make_match(status="live")
        health = data_health_summary()
        self.assertEqual(health["matches_missing_events"], 1)


class DataHealthCountVsListTests(DataHealthFixtureMixin, TestCase):
    """Регрессия: счётчик на карточке должен оставаться точным даже когда
    проблемных матчей больше, чем лимит списка под ней ([:20])."""

    def test_count_not_undercounted_beyond_list_limit(self):
        for i in range(25):
            self._make_match(status="finished", minutes_ago=i)

        health = data_health_summary()
        self.assertEqual(health["matches_missing_events"], 25, "count() не должен зависеть от лимита списка")
        self.assertEqual(len(health["matches_missing_events_list"]), 20, "список обрезан лимитом [:20]")

    def test_list_ordered_by_most_recent_start_time_first(self):
        older = self._make_match(status="finished", minutes_ago=100)
        newer = self._make_match(status="finished", minutes_ago=1)

        health = data_health_summary()
        ids_in_order = [m.id for m in health["matches_missing_events_list"]]
        self.assertLess(ids_in_order.index(newer.id), ids_in_order.index(older.id))


class LastSyncRunTests(TestCase):
    def test_last_run_is_the_most_recent_one(self):
        older = ParserSyncRun.objects.create(
            task_name="update_match_statuses", started_at=timezone.now() - timedelta(hours=1),
            total=10, errors=0,
        )
        newer = ParserSyncRun.objects.create(
            task_name="update_match_statuses", started_at=timezone.now(),
            total=20, errors=2, error_samples=[{"match_id": "abc", "error": "boom"}],
        )
        health = data_health_summary()
        self.assertEqual(health["last_run"].id, newer.id)
        self.assertNotEqual(health["last_run"].id, older.id)

    def test_recent_error_samples_come_from_last_run_only(self):
        ParserSyncRun.objects.create(
            task_name="update_match_statuses", started_at=timezone.now() - timedelta(hours=1),
            total=10, errors=1, error_samples=[{"match_id": "old-run-error"}],
        )
        ParserSyncRun.objects.create(
            task_name="update_match_statuses", started_at=timezone.now(),
            total=10, errors=1, error_samples=[{"match_id": "new-run-error"}],
        )
        health = data_health_summary()
        self.assertEqual(len(health["recent_error_samples"]), 1)
        self.assertEqual(health["recent_error_samples"][0]["match_id"], "new-run-error")

    def test_no_runs_yet_returns_empty_state_not_crash(self):
        health = data_health_summary()
        self.assertIsNone(health["last_run"])
        self.assertEqual(health["recent_error_samples"], [])
