# parsers/sportmonks/tests_tasks.py
"""Тесты задач Sportmonks с моком SportmonksClient: зависший live, VAR-события, ресинк статистики."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from django.test import TestCase

from datetime import timedelta

from django.utils import timezone

from parsers.sportmonks import importers as importers_module
from parsers.sportmonks.importers import import_match_core
from parsers.sportmonks.tasks import sportmonks_resync_recent_stats, sportmonks_update_live
from parsers.tests import _fixture, _make_league, _make_season


class CeleryTaskRegistrationTests(TestCase):
    """Все задачи parsers.sportmonks.tasks зарегистрированы в Celery (см. dopx/celery.py)."""

    def test_sportmonks_tasks_are_registered_in_celery_app(self):
        from dopx.celery import app
        app.finalize()
        registered = set(app.tasks.keys())
        expected = {
            "parsers.sportmonks.tasks.sportmonks_update_live",
            "parsers.sportmonks.tasks.sportmonks_update_upcoming",
            "parsers.sportmonks.tasks.sportmonks_sync_season",
            "parsers.sportmonks.tasks.sportmonks_sync_sidelined",
            "parsers.sportmonks.tasks.sportmonks_sync_coach_activity",
            "parsers.sportmonks.tasks.sportmonks_health_check",
            "parsers.sportmonks.tasks.sportmonks_resync_recent_stats",
        }
        missing = expected - registered
        self.assertEqual(
            missing, set(),
            f"Не зарегистрированы в Celery (см. dopx/celery.py): {missing}",
        )


class SportmonksUpdateLiveStuckMatchReconciliationTests(TestCase):
    """Матч у нас live, но пропал из /livescores/inplay."""

    def setUp(self):
        self.league = _make_league()
        self.season = _make_season(self.league)
        # INPLAY_2ND_HALF -> status='live'.
        fixture = _fixture(sm_id=555000111, dev_name="INPLAY_2ND_HALF")
        self.match = import_match_core(fixture, self.league, self.season)
        self.assertEqual(self.match.status, "live")  # проверка самого хелпера

    @patch("parsers.sportmonks.tasks.SportmonksClient")
    def test_match_missing_from_livescores_gets_heavy_synced_to_finished(self, mock_client_cls):
        """Live-матч, пропавший из inplay, досинхронизируется тяжёлым вызовом."""
        mock_client = MagicMock()
        mock_client.get_livescores.return_value = []  # матч закончился и пропал из inplay
        mock_client.get_fixture.return_value = _fixture(
            sm_id=555000111, dev_name="FT", home_goals=3, away_goals=1,
        )
        mock_client_cls.return_value = mock_client

        sportmonks_update_live()

        self.match.refresh_from_db()
        self.assertEqual(self.match.status, "finished")
        self.assertEqual(self.match.home_score, 3)
        self.assertEqual(self.match.away_score, 1)
        mock_client.get_fixture.assert_called_once()

    @patch("parsers.sportmonks.tasks.SportmonksClient")
    def test_manual_override_match_is_not_touched_even_if_stuck(self, mock_client_cls):
        """manual_override не трогаем."""
        self.match.manual_override = True
        self.match.save(update_fields=["manual_override"])

        mock_client = MagicMock()
        mock_client.get_livescores.return_value = []
        mock_client_cls.return_value = mock_client

        sportmonks_update_live()

        self.match.refresh_from_db()
        self.assertEqual(self.match.status, "live")
        mock_client.get_fixture.assert_not_called()

    @patch("parsers.sportmonks.tasks.SportmonksClient")
    def test_match_still_in_livescores_is_left_to_normal_diff_path(self, mock_client_cls):
        """Матч ещё в inplay — без двойной обработки."""
        mock_client = MagicMock()
        mock_client.get_livescores.return_value = [
            {
                "id": 555000111,
                "league_id": 393,
                "state": {"developer_name": "INPLAY_2ND_HALF"},
                "scores": [
                    {"description": "CURRENT", "score": {"goals": 1, "participant": "home"}},
                    {"description": "CURRENT", "score": {"goals": 0, "participant": "away"}},
                ],
            }
        ]
        mock_client.get_fixture.return_value = _fixture(
            sm_id=555000111, dev_name="INPLAY_2ND_HALF", home_goals=1, away_goals=0,
        )
        mock_client_cls.return_value = mock_client

        sportmonks_update_live()

        # Ровно один heavy sync.
        self.assertEqual(mock_client.get_fixture.call_count, 1)


class SportmonksUpdateLiveEventSignatureTests(TestCase):
    """Тяжёлая догрузка вызывается и при изменении событий (VAR, новые карточки/замены),
    а не только при смене счёта/статуса.
    """

    def setUp(self):
        self.league = _make_league()
        self.season = _make_season(self.league)
        fixture = _fixture(sm_id=850000001, dev_name="INPLAY_2ND_HALF", home_goals=1, away_goals=0)
        self.match = import_match_core(fixture, self.league, self.season)

        from events.models import MatchEvent
        self.existing_event = MatchEvent.objects.create(
            match=self.match, minute=9, event_type="yellow_card", team_side="home",
            sportmonks_id="1", extra_data={"type": {"developer_name": "YELLOWCARD"}},
        )

    def _api_fixture(self, event_dev_name: str) -> dict:
        """Счёт/статус совпадают с базой — меняется только developer_name события."""
        return {
            "id": 850000001,
            "league_id": 393,
            "state": {"developer_name": "INPLAY_2ND_HALF"},
            "scores": [
                {"description": "CURRENT", "score": {"goals": 1, "participant": "home"}},
                {"description": "CURRENT", "score": {"goals": 0, "participant": "away"}},
            ],
            "events": [{"id": 1, "type": {"developer_name": event_dev_name}}],
        }

    @patch("parsers.sportmonks.tasks.SportmonksClient")
    def test_var_type_change_on_same_event_id_triggers_heavy_sync(self, mock_client_cls):
        """Тот же id, YELLOWCARD -> REDCARD — heavy sync вызывается."""
        mock_client = MagicMock()
        mock_client.get_livescores.return_value = [self._api_fixture("REDCARD")]
        mock_client.get_fixture.return_value = _fixture(
            sm_id=850000001, dev_name="INPLAY_2ND_HALF", home_goals=1, away_goals=0,
        )
        mock_client_cls.return_value = mock_client

        sportmonks_update_live()

        mock_client.get_fixture.assert_called_once()

    @patch("parsers.sportmonks.tasks.SportmonksClient")
    def test_identical_events_do_not_trigger_heavy_sync(self, mock_client_cls):
        """Ничего не изменилось — heavy sync не вызывается."""
        mock_client = MagicMock()
        mock_client.get_livescores.return_value = [self._api_fixture("YELLOWCARD")]
        mock_client_cls.return_value = mock_client

        sportmonks_update_live()

        mock_client.get_fixture.assert_not_called()

    @patch("parsers.sportmonks.tasks.SportmonksClient")
    def test_new_event_id_triggers_heavy_sync(self, mock_client_cls):
        """Новое событие (новый id) — heavy sync вызывается."""
        fx = self._api_fixture("YELLOWCARD")
        fx["events"].append({"id": 2, "type": {"developer_name": "SUBSTITUTION"}})
        mock_client = MagicMock()
        mock_client.get_livescores.return_value = [fx]
        mock_client.get_fixture.return_value = _fixture(
            sm_id=850000001, dev_name="INPLAY_2ND_HALF", home_goals=1, away_goals=0,
        )
        mock_client_cls.return_value = mock_client

        sportmonks_update_live()

        mock_client.get_fixture.assert_called_once()


class SportmonksResyncRecentStatsTests(TestCase):
    """Ресинк статистики недавно завершённых матчей (STATS_RESYNC_WINDOW)."""

    def setUp(self):
        self.league = _make_league()
        self.season = _make_season(self.league)

    def _make_finished_match(self, sm_id, hours_ago):
        fixture = _fixture(sm_id=sm_id, dev_name="FT", home_goals=2, away_goals=1)
        match = import_match_core(fixture, self.league, self.season)
        match.start_time = timezone.now() - timedelta(hours=hours_ago)
        match.save(update_fields=["start_time"])
        return match

    @patch("parsers.sportmonks.tasks.SportmonksClient")
    def test_recently_finished_match_gets_stats_resynced_even_without_score_change(self, mock_client_cls):
        """heavy sync вызывается при совпадающем счёте/статусе."""
        self._make_finished_match(sm_id=700000111, hours_ago=1)
        mock_client = MagicMock()
        mock_client.get_fixture.return_value = _fixture(
            sm_id=700000111, dev_name="FT", home_goals=2, away_goals=1,
        )
        mock_client_cls.return_value = mock_client

        sportmonks_resync_recent_stats()

        # sportmonks_id — строка.
        mock_client.get_fixture.assert_called_once_with(
            "700000111", include=importers_module.HEAVY_FIXTURE_INCLUDE,
        )

    @patch("parsers.sportmonks.tasks.SportmonksClient")
    def test_match_older_than_window_is_not_touched(self, mock_client_cls):
        """Матч завершился 4 ч назад (окно 3 ч) — не трогаем."""
        self._make_finished_match(sm_id=700000222, hours_ago=4)
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        sportmonks_resync_recent_stats()

        mock_client.get_fixture.assert_not_called()

    @patch("parsers.sportmonks.tasks.SportmonksClient")
    def test_scheduled_match_is_not_touched(self, mock_client_cls):
        """Несыгранный матч — не трогаем."""
        fixture = _fixture(sm_id=700000333, dev_name="NS")
        match = import_match_core(fixture, self.league, self.season)
        self.assertEqual(match.status, "scheduled")
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        sportmonks_resync_recent_stats()

        mock_client.get_fixture.assert_not_called()
