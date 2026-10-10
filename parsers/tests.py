# parsers/tests.py
"""Тесты импортёра Sportmonks (parsers/sportmonks/importers.py)."""
from __future__ import annotations

from unittest.mock import patch

from datetime import timedelta

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from leagues.models import League
from matches.models import Match
from parsers.sportmonks.importers import get_or_create_player, import_full_fixture, import_match_core
from parsers.tasks import check_sync_errors_and_alert
from players.models import Player
from seasons.models import Season
from teams.models import Team


def _make_league(sportmonks_id: str = "393") -> League:
    return League.objects.create(
        name="Премьер-лига", country="Казахстан", sportmonks_id=sportmonks_id, is_primary=True,
    )


def _make_season(league: League, year: str = "2026", sportmonks_id: str = "27438") -> Season:
    return Season.objects.create(league=league, year=year, sportmonks_id=sportmonks_id, is_active=True)


def _fixture(
    sm_id: int = 19681993,
    league_id: int = 393,
    home_id: int = 1001,
    away_id: int = 1002,
    home_name: str = "Тобол",
    away_name: str = "Кайсар",
    dev_name: str = "FT",
    starting_at: str = "2026-08-25 14:00:00",
    home_goals: int = 2,
    away_goals: int = 1,
    round_name: str = "21",
) -> dict:
    """Минимальный fixture в формате GET /fixtures/{id}."""
    return {
        "id": sm_id,
        "league_id": league_id,
        "participants": [
            {
                "id": home_id, "name": home_name, "image_path": "https://example.com/home.png",
                "meta": {"location": "home"},
            },
            {
                "id": away_id, "name": away_name, "image_path": "https://example.com/away.png",
                "meta": {"location": "away"},
            },
        ],
        "state": {"developer_name": dev_name},
        "starting_at": starting_at,
        "scores": [
            {
                "description": "CURRENT",
                "score": {"goals": home_goals, "participant": "home"},
            },
            {
                "description": "CURRENT",
                "score": {"goals": away_goals, "participant": "away"},
            },
        ],
        "round": {"name": round_name},
        "referees": [],
    }


def _recent_start(hours_ago: int = 2) -> str:
    """starting_at недавнего матча — голосование по нему ещё открыто."""
    from datetime import datetime, timedelta, timezone as dt_tz
    return (datetime.now(dt_tz.utc) - timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M:%S")


class ImportMatchCoreTests(TestCase):
    """import_match_core: создание, идемпотентность, manual_override, защита от чужой лиги."""

    def setUp(self):
        self.league = _make_league()
        self.season = _make_season(self.league)

    def test_creates_match_with_correct_fields(self):
        fixture = _fixture()
        match = import_match_core(fixture, self.league, self.season)

        self.assertEqual(match.sportmonks_id, "19681993")
        self.assertEqual(match.league_id, self.league.id)
        self.assertEqual(match.season_id, self.season.id)
        self.assertEqual(match.home_team.name, "Тобол")
        self.assertEqual(match.away_team.name, "Кайсар")
        self.assertEqual(match.status, "finished")
        self.assertEqual(match.home_score, 2)
        self.assertEqual(match.away_score, 1)
        self.assertEqual(match.tour, 21)
        # У finished-матча должны быть end_time и voting_open_until
        self.assertIsNotNone(match.end_time)
        self.assertIsNotNone(match.voting_open_until)

    def test_reimport_is_idempotent_no_duplicate_matches(self):
        fixture = _fixture()
        first = import_match_core(fixture, self.league, self.season)
        second = import_match_core(fixture, self.league, self.season)

        self.assertEqual(first.id, second.id)
        self.assertEqual(Match.objects.filter(sportmonks_id="19681993").count(), 1)
        # Команды не дублируются при повторном импорте.
        self.assertEqual(Team.objects.filter(sportmonks_id="1001").count(), 1)
        self.assertEqual(Team.objects.filter(sportmonks_id="1002").count(), 1)

    def test_finished_match_score_change_records_discrepancy(self):
        """Счёт завершённого матча поменялся при синке — пишем ParserDiscrepancy."""
        from parsers.models import ParserDiscrepancy

        import_match_core(_fixture(), self.league, self.season)
        changed = _fixture()
        changed["scores"][0]["score"]["goals"] = 3
        import_match_core(changed, self.league, self.season)

        d = ParserDiscrepancy.objects.get()
        self.assertEqual((d.field_name, d.old_value, d.new_value), ("home_score", "2", "3"))
        self.assertEqual(d.field_label, "Голы хозяев")

        # Повторный синк с тем же счётом — новых записей нет.
        import_match_core(changed, self.league, self.season)
        self.assertEqual(ParserDiscrepancy.objects.count(), 1)

    def test_live_score_progress_is_not_discrepancy(self):
        """Обычный ход матча (live) расхождением не считается."""
        from parsers.models import ParserDiscrepancy

        import_match_core(_fixture(dev_name="INPLAY_1ST_HALF", home_goals=0, away_goals=0), self.league, self.season)
        import_match_core(_fixture(), self.league, self.season)
        self.assertFalse(ParserDiscrepancy.objects.exists())

    def test_reimport_syncs_logo_url_freely(self):
        """logo_url всегда синкается свежим значением с источника."""
        fixture = _fixture()
        match = import_match_core(fixture, self.league, self.season)
        team = match.home_team
        self.assertEqual(team.logo_url, "https://example.com/home.png")

        updated_fixture = _fixture()
        updated_fixture["participants"][0]["image_path"] = "https://example.com/home-updated.png"
        import_match_core(updated_fixture, self.league, self.season)
        team.refresh_from_db()
        self.assertEqual(team.logo_url, "https://example.com/home-updated.png")

    def test_reimport_preserves_manually_uploaded_logo(self):
        """Загруженный вручную Team.logo импорт не трогает."""
        from django.core.files.uploadedfile import SimpleUploadedFile

        fixture = _fixture()
        match = import_match_core(fixture, self.league, self.season)
        team = match.home_team
        team.logo = SimpleUploadedFile("staff-logo.png", b"fake-png-bytes", content_type="image/png")
        team.save(update_fields=["logo"])

        import_match_core(_fixture(), self.league, self.season)
        team.refresh_from_db()
        self.assertTrue(team.logo)
        self.assertIn("staff-logo", team.logo.name)
        self.assertEqual(team.logo_display, team.logo.url)

    def test_manual_override_prevents_status_and_date_overwrite(self):
        fixture = _fixture(dev_name="NS")
        match = import_match_core(fixture, self.league, self.season)
        self.assertEqual(match.status, "scheduled")

        match.manual_override = True
        match.status = "postponed"
        match.save(update_fields=["manual_override", "status"])
        original_start = match.start_time

        # manual_override блокирует перезапись статуса и даты.
        updated_fixture = _fixture(dev_name="FT", starting_at="2026-09-01 10:00:00")
        result = import_match_core(updated_fixture, self.league, self.season)

        self.assertEqual(result.id, match.id)
        self.assertEqual(result.status, "postponed")
        self.assertEqual(result.start_time, original_start)

    def test_foreign_league_fixture_is_rejected(self):
        """Fixture чужой лиги отклоняется с ValueError."""
        foreign_fixture = _fixture(league_id=999)
        with self.assertRaises(ValueError):
            import_match_core(foreign_fixture, self.league, self.season)
        self.assertFalse(Match.objects.filter(sportmonks_id="19681993").exists())

    def test_missing_league_id_in_fixture_does_not_raise(self):
        """Отсутствие league_id — не ошибка."""
        fixture = _fixture()
        del fixture["league_id"]
        match = import_match_core(fixture, self.league, self.season)
        self.assertEqual(match.league_id, self.league.id)

    def test_unknown_state_defaults_to_scheduled_without_crashing(self):
        """Неизвестный статус — warning и 'scheduled', без падения."""
        fixture = _fixture(dev_name="SOME_NEW_STATE_CODE")
        match = import_match_core(fixture, self.league, self.season)
        self.assertEqual(match.status, "scheduled")

    def test_missing_participants_raises_value_error(self):
        fixture = _fixture()
        fixture["participants"] = []
        with self.assertRaises(ValueError):
            import_match_core(fixture, self.league, self.season)

    def test_cancelled_state_maps_to_cancelled_status(self):
        fixture = _fixture(dev_name="CANCELLED")
        match = import_match_core(fixture, self.league, self.season)
        self.assertEqual(match.status, "cancelled")


def _goal_event(
    minute: int = 87, participant_id: int = 1001, dev_name: str = "GOAL",
    event_id: int = 90000001,
) -> dict:
    """Минимальное событие Sportmonks. event_id — ключ сопоставления при повторном импорте."""
    return {
        "id": event_id,
        "type": {"developer_name": dev_name},
        "minute": minute,
        "participant_id": participant_id,
        "player_id": None,
        "related_player_id": None,
        "result": "1-0",
        "extra_minute": 0,
    }


class ImportFullFixtureNotificationWiringTests(TestCase):
    """import_full_fixture ставит пуш-задачи в очередь.
    captureOnCommitCallbacks(execute=True) обязателен — иначе on_commit не выполнится.
    """

    def setUp(self):
        self.league = _make_league()
        self.season = _make_season(self.league)

    @patch("notifications.tasks.notify_followers_match_activity.delay")
    def test_first_transition_to_finished_queues_activity_notification(self, mock_delay):
        fixture = _fixture(sm_id=777000111, dev_name="INPLAY_2ND_HALF", starting_at=_recent_start())
        match = import_full_fixture(fixture, self.league, self.season)
        self.assertEqual(match.status, "live")
        mock_delay.assert_not_called()

        finished_fixture = _fixture(sm_id=777000111, dev_name="FT", starting_at=_recent_start())
        with self.captureOnCommitCallbacks(execute=True):
            match = import_full_fixture(finished_fixture, self.league, self.season)

        self.assertEqual(match.status, "finished")
        mock_delay.assert_called_once_with(str(match.id))

    @patch("notifications.tasks.notify_followers_match_activity.delay")
    def test_reimporting_already_finished_match_does_not_requeue(self, mock_delay):
        """Досинк завершённого матча не шлёт повторное приглашение оценить."""
        fixture = _fixture(sm_id=777000222, dev_name="FT", starting_at=_recent_start())
        with self.captureOnCommitCallbacks(execute=True):
            import_full_fixture(fixture, self.league, self.season)
        self.assertEqual(mock_delay.call_count, 1)

        with self.captureOnCommitCallbacks(execute=True):
            import_full_fixture(_fixture(sm_id=777000222, dev_name="FT", starting_at=_recent_start()), self.league, self.season)
        self.assertEqual(mock_delay.call_count, 1)

    @patch("notifications.tasks.notify_followers_match_activity.delay")
    def test_old_finished_match_backfill_does_not_invite_to_vote(self, mock_delay):
        """Бэкафилл старого матча (голосование закрыто) — приглашения оценить нет."""
        with self.captureOnCommitCallbacks(execute=True):
            import_full_fixture(_fixture(sm_id=777009001, dev_name="FT"), self.league, self.season)
        mock_delay.assert_not_called()

    @patch("notifications.tasks.notify_followers_match_event.delay")
    def test_goals_of_finished_match_do_not_queue_live_push(self, mock_delay):
        """Досинк завершённого матча не присылает пачку старых голов."""
        fixture = _fixture(sm_id=777009002, dev_name="FT", starting_at=_recent_start())
        fixture["events"] = [_goal_event(minute=87, participant_id=fixture["participants"][0]["id"])]
        with self.captureOnCommitCallbacks(execute=True):
            import_full_fixture(fixture, self.league, self.season)
        mock_delay.assert_not_called()

    @patch("notifications.tasks.notify_followers_match_event.delay")
    @patch("notifications.tasks.notify_followers_match_activity.delay")
    def test_quiet_import_sends_nothing(self, mock_activity, mock_event):
        """Переимпорт и проигрывание из архива (quiet) — без рассылок."""
        from core.bulk import quiet

        fixture = _fixture(sm_id=777009003, dev_name="INPLAY_2ND_HALF", starting_at=_recent_start())
        fixture["events"] = [_goal_event(minute=87, participant_id=fixture["participants"][0]["id"])]
        with quiet(), self.captureOnCommitCallbacks(execute=True):
            import_full_fixture(fixture, self.league, self.season)
            import_full_fixture(_fixture(sm_id=777009003, dev_name="FT", starting_at=_recent_start()), self.league, self.season)
        mock_event.assert_not_called()
        mock_activity.assert_not_called()

    @patch("notifications.tasks.notify_followers_match_event.delay")
    def test_new_goal_event_queues_push_worthy_notification(self, mock_delay):
        # Матч ещё идёт — чтобы не сработало activity-уведомление о завершении.
        fixture = _fixture(sm_id=777000333, dev_name="INPLAY_2ND_HALF")
        fixture["events"] = [_goal_event(minute=87, participant_id=fixture["participants"][0]["id"])]

        with self.captureOnCommitCallbacks(execute=True):
            match = import_full_fixture(fixture, self.league, self.season)

        mock_delay.assert_called_once()
        called_match_id, called_event_id = mock_delay.call_args.args
        self.assertEqual(called_match_id, str(match.id))
        from events.models import MatchEvent
        event = MatchEvent.objects.get(match=match, minute=87, event_type="goal")
        self.assertEqual(called_event_id, str(event.id))

    @patch("notifications.tasks.notify_followers_match_event.delay")
    def test_non_push_worthy_event_type_does_not_queue(self, mock_delay):
        """Жёлтая карточка сохраняется, но пуш не шлёт."""
        fixture = _fixture(sm_id=777000444, dev_name="INPLAY_2ND_HALF")
        fixture["events"] = [_goal_event(minute=54, participant_id=fixture["participants"][0]["id"], dev_name="YELLOWCARD")]

        with self.captureOnCommitCallbacks(execute=True):
            import_full_fixture(fixture, self.league, self.season)

        mock_delay.assert_not_called()

    @patch("notifications.tasks.notify_followers_match_event.delay")
    def test_reimporting_same_event_does_not_requeue(self, mock_delay):
        """Повторный импорт того же гола не шлёт второй пуш."""
        fixture = _fixture(sm_id=777000555, dev_name="INPLAY_2ND_HALF")
        fixture["events"] = [_goal_event(minute=23, participant_id=fixture["participants"][0]["id"])]

        with self.captureOnCommitCallbacks(execute=True):
            import_full_fixture(fixture, self.league, self.season)
        self.assertEqual(mock_delay.call_count, 1)

        with self.captureOnCommitCallbacks(execute=True):
            import_full_fixture(fixture, self.league, self.season)
        self.assertEqual(mock_delay.call_count, 1)

    @patch("notifications.tasks.notify_followers_match_started.delay")
    def test_first_transition_to_live_queues_started_notification(self, mock_delay):
        """Переход scheduled -> live шлёт пуш «матч начался»."""
        scheduled_fixture = _fixture(sm_id=777000666, dev_name="NS")
        match = import_full_fixture(scheduled_fixture, self.league, self.season)
        self.assertEqual(match.status, "scheduled")
        mock_delay.assert_not_called()

        live_fixture = _fixture(sm_id=777000666, dev_name="INPLAY_1ST_HALF")
        with self.captureOnCommitCallbacks(execute=True):
            match = import_full_fixture(live_fixture, self.league, self.season)

        self.assertEqual(match.status, "live")
        mock_delay.assert_called_once_with(str(match.id))

    @patch("notifications.tasks.notify_followers_match_started.delay")
    def test_reimporting_already_live_match_does_not_requeue_started(self, mock_delay):
        scheduled_fixture = _fixture(sm_id=777000777, dev_name="NS")
        import_full_fixture(scheduled_fixture, self.league, self.season)

        live_fixture = _fixture(sm_id=777000777, dev_name="INPLAY_1ST_HALF")
        with self.captureOnCommitCallbacks(execute=True):
            import_full_fixture(live_fixture, self.league, self.season)
        self.assertEqual(mock_delay.call_count, 1)

        # Повторный тик live-матча не шлёт «матч начался» снова.
        with self.captureOnCommitCallbacks(execute=True):
            import_full_fixture(
                _fixture(sm_id=777000777, dev_name="INPLAY_2ND_HALF"), self.league, self.season,
            )
        self.assertEqual(mock_delay.call_count, 1)

    @patch("notifications.tasks.notify_followers_match_started.delay")
    def test_match_created_directly_as_live_does_not_fire_started(self, mock_delay):
        """Первый импорт сразу в live (бэкафилл) — пуша нет."""
        fixture = _fixture(sm_id=777000888, dev_name="INPLAY_1ST_HALF")
        with self.captureOnCommitCallbacks(execute=True):
            import_full_fixture(fixture, self.league, self.season)
        mock_delay.assert_not_called()

    @patch("notifications.tasks.notify_followers_match_started.delay")
    def test_match_skipping_straight_to_finished_does_not_fire_started(self, mock_delay):
        """scheduled -> finished без live — «матч начался» не шлём."""
        scheduled_fixture = _fixture(sm_id=777000999, dev_name="NS")
        import_full_fixture(scheduled_fixture, self.league, self.season)

        finished_fixture = _fixture(sm_id=777000999, dev_name="FT")
        with self.captureOnCommitCallbacks(execute=True):
            import_full_fixture(finished_fixture, self.league, self.season)
        mock_delay.assert_not_called()

    @patch("parsers.sportmonks.importers.import_lineups")
    @patch("notifications.tasks.notify_followers_lineups_available.delay")
    def test_lineups_becoming_available_queues_notification(self, mock_delay, mock_import_lineups):
        """has_lineup False -> True ставит пуш «составы объявлены»."""
        def _fake_import_lineups(match, lineups_data, formations_data=None, events_data=None):
            match.has_lineup = True
            match.save(update_fields=["has_lineup", "updated_at"])
            return True
        mock_import_lineups.side_effect = _fake_import_lineups

        fixture = _fixture(sm_id=777001111, dev_name="NS")
        fixture["lineups"] = [{"dummy": "нужен непустой список — сам импорт мокнут выше"}]

        with self.captureOnCommitCallbacks(execute=True):
            match = import_full_fixture(fixture, self.league, self.season)

        self.assertTrue(match.has_lineup)
        mock_delay.assert_called_once_with(str(match.id))

    @patch("parsers.sportmonks.importers.import_lineups")
    @patch("notifications.tasks.notify_followers_lineups_available.delay")
    def test_reimporting_with_lineups_already_present_does_not_requeue(self, mock_delay, mock_import_lineups):
        def _fake_import_lineups(match, lineups_data, formations_data=None, events_data=None):
            match.has_lineup = True
            match.save(update_fields=["has_lineup", "updated_at"])
            return True
        mock_import_lineups.side_effect = _fake_import_lineups

        fixture = _fixture(sm_id=777001222, dev_name="NS")
        fixture["lineups"] = [{"dummy": "1"}]
        with self.captureOnCommitCallbacks(execute=True):
            import_full_fixture(fixture, self.league, self.season)
        self.assertEqual(mock_delay.call_count, 1)

        with self.captureOnCommitCallbacks(execute=True):
            import_full_fixture(fixture, self.league, self.season)
        self.assertEqual(mock_delay.call_count, 1)

    @patch("parsers.sportmonks.importers.import_lineups")
    @patch("notifications.tasks.notify_followers_lineups_available.delay")
    def test_lineups_available_not_fired_for_already_finished_match(self, mock_delay, mock_import_lineups):
        """Составы вместе с завершённым матчем — пуша нет."""
        def _fake_import_lineups(match, lineups_data, formations_data=None, events_data=None):
            match.has_lineup = True
            match.save(update_fields=["has_lineup", "updated_at"])
            return True
        mock_import_lineups.side_effect = _fake_import_lineups

        fixture = _fixture(sm_id=777001333, dev_name="FT")
        fixture["lineups"] = [{"dummy": "1"}]
        with self.captureOnCommitCallbacks(execute=True):
            match = import_full_fixture(fixture, self.league, self.season)

        self.assertEqual(match.status, "finished")
        self.assertTrue(match.has_lineup)
        mock_delay.assert_not_called()

    @patch("notifications.tasks.notify_followers_match_event.delay")
    def test_goal_disallowed_event_is_imported_and_queues_push(self, mock_delay):
        """GOAL_DISALLOWED маппится и шлёт пуш «гол отменён»."""
        fixture = _fixture(sm_id=777001444, dev_name="INPLAY_2ND_HALF")
        fixture["events"] = [
            _goal_event(minute=54, participant_id=fixture["participants"][0]["id"], dev_name="GOAL_DISALLOWED"),
        ]

        with self.captureOnCommitCallbacks(execute=True):
            match = import_full_fixture(fixture, self.league, self.season)

        from events.models import MatchEvent
        event = MatchEvent.objects.get(match=match, minute=54)
        self.assertEqual(event.event_type, "disallowed_goal")
        mock_delay.assert_called_once_with(str(match.id), str(event.id))

    @patch("notifications.tasks.notify_followers_match_event.delay")
    def test_var_card_upgrade_updates_same_event_no_duplicate(self, mock_delay):
        """VAR: жёлтая -> красная с тем же event id обновляет одну запись, а не создаёт вторую."""
        fixture = _fixture(sm_id=777001555, dev_name="INPLAY_2ND_HALF")
        fixture["events"] = [
            _goal_event(minute=9, participant_id=fixture["participants"][0]["id"],
                        dev_name="YELLOWCARD", event_id=55123456),
        ]
        with self.captureOnCommitCallbacks(execute=True):
            match = import_full_fixture(fixture, self.league, self.season)

        from events.models import MatchEvent
        self.assertEqual(MatchEvent.objects.filter(match=match).count(), 1)
        event = MatchEvent.objects.get(match=match)
        self.assertEqual(event.event_type, "yellow_card")
        event_pk = event.id
        # Жёлтая — без пуша.
        mock_delay.assert_not_called()

        # VAR меняет то же событие (тот же id) на красную.
        fixture["events"] = [
            _goal_event(minute=11, participant_id=fixture["participants"][0]["id"],
                        dev_name="REDCARD", event_id=55123456),
        ]
        with self.captureOnCommitCallbacks(execute=True):
            import_full_fixture(fixture, self.league, self.season)

        self.assertEqual(MatchEvent.objects.filter(match=match).count(), 1, "не должно быть дубля")
        event.refresh_from_db()
        self.assertEqual(event.id, event_pk, "должна обновиться та же запись, не создаться новая")
        self.assertEqual(event.event_type, "red_card")
        self.assertEqual(event.minute, 11)
        # Красная — пуш должен уйти.
        mock_delay.assert_called_once_with(str(match.id), str(event_pk))

    def test_blank_resend_of_same_event_id_does_not_erase_real_data(self):
        """«Пустой» повтор события с тем же id не затирает уже сохранённые данные игрока."""
        from events.models import MatchEvent
        from players.models import Player

        scorer = Player.objects.create(first_name="Урош", last_name="Милованович", sportmonks_id="784559")

        fixture = _fixture(sm_id=777002666, dev_name="INPLAY_2ND_HALF")
        real_goal_event = {
            "id": 157899217,
            "type": {"developer_name": "GOAL"},
            "minute": 44,
            "participant_id": fixture["participants"][1]["id"],
            "player_id": 784559,
            "related_player_id": None,
            "result": "1-2",
            "extra_minute": 0,
            "player_name": "Uros Milovanović",
        }
        fixture["events"] = [real_goal_event]
        with self.captureOnCommitCallbacks(execute=True):
            match = import_full_fixture(fixture, self.league, self.season)

        event = MatchEvent.objects.get(match=match)
        self.assertEqual(event.player_id, scorer.id)
        event_pk = event.id

        # Тот же id, минута +1, поля игрока пустые — как в реальном ответе API.
        blank_resend_event = {
            "id": 157899217,
            "type": {"developer_name": "GOAL"},
            "minute": 45,
            "participant_id": fixture["participants"][1]["id"],
            "player_id": None,
            "related_player_id": None,
            "result": "1-2",
            "extra_minute": None,
            "player_name": None,
        }
        fixture["events"] = [blank_resend_event]
        with self.captureOnCommitCallbacks(execute=True):
            import_full_fixture(fixture, self.league, self.season)

        self.assertEqual(
            MatchEvent.objects.filter(match=match).count(), 1,
            "пустой повтор не должен создавать вторую строку",
        )
        event.refresh_from_db()
        self.assertEqual(event.id, event_pk)
        self.assertEqual(event.player_id, scorer.id, "пустой повтор не должен стирать уже известного игрока")
        self.assertEqual(event.minute, 44, "пустой повтор не должен сдвигать минуту настоящего события")

    def test_goal_corrected_to_own_goal_stays_one_event(self):
        """Правка гола в автогол с тем же id — одна запись, и в одном пакете, и в следующем."""
        from events.models import MatchEvent
        from parsers.sportmonks.importers import import_events

        fixture = _fixture(sm_id=777003778, dev_name="FT")
        match = import_match_core(fixture, self.league, self.season)
        home = fixture["participants"][0]["id"]

        def goal(dev_name):
            return {"id": 157943803, "type": {"developer_name": dev_name}, "minute": 23, "participant_id": home,
                    "player_id": None, "player_name": "X", "related_player_id": None, "result": "1-0",
                    "extra_minute": None}

        import_events(match, [goal("GOAL")])
        import_events(match, [goal("GOAL"), goal("OWNGOAL")])  # поставщик прислал обе версии
        self.assertEqual(list(MatchEvent.objects.filter(match=match).values_list("event_type", flat=True)),
                         ["own_goal"])

    def test_withdrawn_goal_removed_but_not_on_truncated_payload(self):
        from events.models import MatchEvent
        from parsers.sportmonks.importers import import_events

        fixture = _fixture(sm_id=777003780, dev_name="FT")
        match = import_match_core(fixture, self.league, self.season)
        away = fixture["participants"][1]["id"]

        def goal(sm_id, minute):
            return {"id": sm_id, "type": {"developer_name": "GOAL"}, "minute": minute, "participant_id": away,
                    "player_id": None, "player_name": "X", "related_player_id": None, "result": "0-1",
                    "extra_minute": None}

        events = [goal(i, 10 + i) for i in range(1, 11)]
        import_events(match, events)
        import_events(match, events[1:])  # поставщик снял первый гол
        self.assertEqual(MatchEvent.objects.filter(match=match).count(), 9)
        import_events(match, events[1:3])  # обрезанный ответ: пропало 7 из 9 — не трогаем
        self.assertEqual(MatchEvent.objects.filter(match=match).count(), 9)

    def test_lineup_rows_without_player_id_are_kept(self):
        """Строка состава без id: по id из событий, иначе по имени; повтор не плодит дублей."""
        from lineups.models import MatchLineupPlayer
        from parsers.sportmonks.importers import import_lineups
        from players.models import Player

        fixture = _fixture(sm_id=777003781, dev_name="FT")
        match = import_match_core(fixture, self.league, self.season)
        home = fixture["participants"][0]["id"]
        rows = [{"id": 1, "team_id": home, "type_id": 11, "player_id": None, "player": None,
                 "player_name": "Justice Kenesbek", "jersey_number": 5},
                {"id": 2, "team_id": home, "type_id": 11, "player_id": None, "player": None,
                 "player_name": "Everton Moraes", "jersey_number": 9}]
        events = [{"player_id": 37548181, "player_name": "Everton Moraes"}]
        import_lineups(match, rows, events_data=events)
        import_lineups(match, rows, events_data=events)  # повторный синк
        self.assertEqual(MatchLineupPlayer.objects.filter(lineup__match=match, is_starting=True).count(), 2)
        self.assertTrue(Player.objects.filter(sportmonks_id="37548181").exists())
        self.assertEqual(Player.objects.filter(sportmonks_id__isnull=True, team=match.home_team).count(), 1)

    def test_unlinked_player_gets_id_later_without_duplicate(self):
        from parsers.sportmonks.importers import get_or_create_player, get_or_create_unlinked_player
        from players.models import Player

        match = import_match_core(_fixture(sm_id=777003782, dev_name="FT"), self.league, self.season)
        placeholder = get_or_create_unlinked_player("Иван Петров", match.home_team)
        linked = get_or_create_player({"id": 555, "firstname": "Иван", "lastname": "Петров"}, team=match.home_team)
        self.assertEqual(linked.pk, placeholder.pk)
        self.assertEqual(Player.objects.filter(last_name="Петров").count(), 1)

    def test_card_side_taken_from_lineup(self):
        from lineups.models import MatchLineup, MatchLineupPlayer
        from parsers.sportmonks.importers import import_events
        from players.models import Player

        fixture = _fixture(sm_id=777003783, dev_name="FT")
        match = import_match_core(fixture, self.league, self.season)
        player = Player.objects.create(first_name="Лев", last_name="Кургин", team=match.away_team, sportmonks_id="9001")
        lineup = MatchLineup.objects.create(match=match, team=match.away_team, side="away")
        MatchLineupPlayer.objects.create(lineup=lineup, player=player, is_starting=True)
        card = {"id": 1, "type": {"developer_name": "YELLOWCARD"}, "minute": 31, "player_id": 9001,
                "participant_id": fixture["participants"][0]["id"], "extra_minute": None}  # поставщик: хозяева
        import_events(match, [card])
        self.assertEqual(match.events.get().team_side, "away")

    def test_duplicate_event_rejected_by_db(self):
        from django.db import IntegrityError, transaction

        from events.models import MatchEvent

        match = import_match_core(_fixture(sm_id=777003779, dev_name="FT"), self.league, self.season)
        MatchEvent.objects.create(match=match, minute=1, event_type="goal", team_side="home", sportmonks_id="1")
        with self.assertRaises(IntegrityError), transaction.atomic():
            MatchEvent.objects.create(match=match, minute=2, event_type="goal", team_side="home", sportmonks_id="1")

    def test_substitute_inherits_zone_from_outgoing_player(self):
        """Вышедший на замену наследует зону (L/C/R) заменённого игрока."""
        from lineups.models import MatchLineup, MatchLineupPlayer
        from parsers.sportmonks.importers import import_events
        from players.models import Player

        fixture = _fixture(sm_id=777003777, dev_name="INPLAY_2ND_HALF")
        match = import_match_core(fixture, self.league, self.season)

        outgoing = Player.objects.create(
            first_name="Уходит", last_name="Игрок", team=match.home_team, sportmonks_id="500001",
        )
        incoming = Player.objects.create(
            first_name="Выходит", last_name="Заменой", team=match.home_team, sportmonks_id="500002",
        )
        lineup = MatchLineup.objects.create(match=match, team=match.home_team, side="home")
        MatchLineupPlayer.objects.create(
            lineup=lineup, player=outgoing, is_starting=True, position="M", field_position="R",
        )
        MatchLineupPlayer.objects.create(
            lineup=lineup, player=incoming, is_starting=False, position="M", field_position="",
        )

        sub_event = {
            "id": 88800001,
            "type": {"developer_name": "SUBSTITUTION"},
            "minute": 60,
            "participant_id": fixture["participants"][0]["id"],
            "player_id": 500002,
            "related_player_id": 500001,
            "result": "0-0",
            "extra_minute": 0,
        }
        import_events(match, [sub_event])

        incoming_row = MatchLineupPlayer.objects.get(lineup=lineup, player=incoming)
        outgoing_row = MatchLineupPlayer.objects.get(lineup=lineup, player=outgoing)
        self.assertEqual(incoming_row.field_position, "R", "должен унаследовать зону вышедшего")
        self.assertEqual(incoming_row.minute_in, 60)
        self.assertEqual(outgoing_row.minute_out, 60)

    def test_substitute_existing_zone_is_not_overwritten(self):
        """Уже известная зона вошедшего не затирается."""
        from lineups.models import MatchLineup, MatchLineupPlayer
        from parsers.sportmonks.importers import import_events
        from players.models import Player

        fixture = _fixture(sm_id=777003888, dev_name="INPLAY_2ND_HALF")
        match = import_match_core(fixture, self.league, self.season)

        outgoing = Player.objects.create(
            first_name="Уходит2", last_name="Игрок", team=match.home_team, sportmonks_id="500003",
        )
        incoming = Player.objects.create(
            first_name="Выходит2", last_name="Заменой", team=match.home_team, sportmonks_id="500004",
        )
        lineup = MatchLineup.objects.create(match=match, team=match.home_team, side="home")
        MatchLineupPlayer.objects.create(
            lineup=lineup, player=outgoing, is_starting=True, position="M", field_position="L",
        )
        MatchLineupPlayer.objects.create(
            lineup=lineup, player=incoming, is_starting=False, position="RM", field_position="R",
        )

        sub_event = {
            "id": 88800002,
            "type": {"developer_name": "SUBSTITUTION"},
            "minute": 70,
            "participant_id": fixture["participants"][0]["id"],
            "player_id": 500004,
            "related_player_id": 500003,
            "result": "0-0",
            "extra_minute": 0,
        }
        import_events(match, [sub_event])

        incoming_row = MatchLineupPlayer.objects.get(lineup=lineup, player=incoming)
        self.assertEqual(incoming_row.field_position, "R", "уже известная зона не должна перезаписываться")


class DecidedAdministrativelyTests(TestCase):
    """Технические поражения без составов не считаются ошибкой синка."""

    def setUp(self):
        self.league = _make_league()
        self.season = _make_season(self.league)

    def test_awarded_state_sets_flag(self):
        match = import_match_core(_fixture(dev_name="AWARDED"), self.league, self.season)
        self.assertTrue(match.decided_administratively)

    def test_walkover_state_sets_flag(self):
        match = import_match_core(_fixture(dev_name="WO"), self.league, self.season)
        self.assertTrue(match.decided_administratively)

    def test_abandoned_is_not_a_result(self):
        # Прерванный матч переигрывают или присуждают позже (AWARDED) — пока это не результат.
        match = import_match_core(_fixture(dev_name="ABANDONED"), self.league, self.season)
        self.assertFalse(match.decided_administratively)
        self.assertEqual(match.status, "postponed")

    def test_normal_finished_match_does_not_set_flag(self):
        match = import_match_core(_fixture(dev_name="FT"), self.league, self.season)
        self.assertFalse(match.decided_administratively)

    @patch("parsers.tasks._send_sync_error_alert")
    def test_alert_excludes_walkover_matches_without_lineup(self, mock_alert):
        """6 технических матчей без состава — алерта нет."""
        for i in range(6):
            match = import_match_core(_fixture(sm_id=800000000 + i, dev_name="WO", starting_at=_hours_ago(4)), self.league, self.season)
            self.assertFalse(match.has_lineup)

        result = check_sync_errors_and_alert()
        self.assertEqual(result, {"status": "ok"})
        mock_alert.assert_not_called()

    @patch("parsers.tasks._send_sync_error_alert")
    def test_alert_still_fires_for_genuine_missing_lineups(self, mock_alert):
        """Контроль: обычные матчи без состава — алерт есть."""
        for i in range(6):
            import_match_core(_fixture(sm_id=810000000 + i, dev_name="FT", starting_at=_hours_ago(4)), self.league, self.season)

        result = check_sync_errors_and_alert()
        self.assertEqual(result["status"], "alert_sent")
        mock_alert.assert_called_once()

    @patch("parsers.tasks._send_sync_error_alert")
    def test_alert_counts_by_match_time_not_record_time(self, mock_alert):
        """Старые матчи из бэкафилла и только что сыгранные (поставщик ещё дозаливает) — без алерта."""
        for i in range(3):
            import_match_core(_fixture(sm_id=820000000 + i, dev_name="FT", starting_at="2025-05-01 14:00:00"), self.league, self.season)
            import_match_core(_fixture(sm_id=830000000 + i, dev_name="FT", starting_at=_hours_ago(1)), self.league, self.season)
        self.assertEqual(check_sync_errors_and_alert(), {"status": "ok"})
        mock_alert.assert_not_called()


def _hours_ago(hours: int) -> str:
    """Время начала матча в формате Sportmonks (UTC), hours часов назад."""
    from datetime import datetime, timedelta, timezone as dt_tz
    return (datetime.now(dt_tz.utc) - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")


def _sportmonks_player(
    sm_id: int, firstname: str = "", lastname: str = "", name: str = "", display_name: str = "",
) -> dict:
    return {
        "id": sm_id, "firstname": firstname, "lastname": lastname,
        "name": name, "display_name": display_name, "position_id": None,
    }


class PlayerNameCorrectionTests(TestCase):
    """PLAYER_NAME_CORRECTIONS переживает повторный импорт с теми же неверными данными."""

    def test_cyrillic_wrong_name_from_sportmonks_firstname_lastname_is_corrected(self):
        """Источник шлёт готовую неверную кириллицу — поправка срабатывает."""
        player_data = _sportmonks_player(9001001, firstname="Эркин", lastname="Тапалов")
        player = get_or_create_player(player_data)
        self.assertEqual(player.first_name, "Еркин")
        self.assertEqual(player.last_name, "Тапалов")

    def test_correction_survives_reimport_with_same_wrong_raw_data(self):
        """Повторный импорт не откатывает исправление."""
        player_data = _sportmonks_player(9001002, firstname="Эркин", lastname="Тапалов")
        get_or_create_player(player_data)

        # Второй синк с тем же неверным «Эркин».
        player = get_or_create_player(_sportmonks_player(9001002, firstname="Эркин", lastname="Тапалов"))
        self.assertEqual(player.first_name, "Еркин")

        self.assertEqual(Player.objects.filter(sportmonks_id="9001002").count(), 1)

    def test_manually_corrected_db_record_is_not_reverted_by_next_sync(self):
        """В базе уже исправленное имя, прилетает обычный синк — имя не откатывается."""
        Player.objects.create(sportmonks_id="9001003", first_name="Еркин", last_name="Тапалов")
        player = get_or_create_player(_sportmonks_player(9001003, firstname="Эркин", lastname="Тапалов"))
        self.assertEqual(player.first_name, "Еркин")

    def test_transliteration_typo_is_also_corrected_via_same_mechanism(self):
        """«Rafael» -> «Рафаел» ловится тем же словарём по результату транслитерации."""
        player = get_or_create_player(_sportmonks_player(9001004, firstname="Rafael", lastname="Testov"))
        self.assertEqual(player.first_name, "Рафаэль")

    def test_unrelated_slavic_name_is_not_affected(self):
        """«Павел» поправка не задевает."""
        player = get_or_create_player(_sportmonks_player(9001005, firstname="Pavel", lastname="Testov"))
        self.assertEqual(player.first_name, "Павел")
        self.assertEqual(player.first_name, "Павел")


class SportmonksPhotoTests(TestCase):
    PHOTO = "https://cdn.sportmonks.com/images/soccer/players/21/136053.png"
    PLACEHOLDER = "https://cdn.sportmonks.com/images/soccer/placeholder.png"

    def test_player_photo_set_and_not_erased_by_placeholder(self):
        data = {"id": 555, "name": "Иван Петров", "common_name": "Иван Петров", "image_path": self.PLACEHOLDER}
        player = get_or_create_player(data)
        self.assertEqual(player.photo_url, "")
        self.assertIsNone(player.photo_display)
        player = get_or_create_player({**data, "image_path": self.PHOTO})
        self.assertEqual(player.photo_display, self.PHOTO)
        player = get_or_create_player(data)
        self.assertEqual(player.photo_url, self.PHOTO)

    def test_referee_photo_updated_on_repeat_sync(self):
        from parsers.sportmonks.importers import get_or_create_referee
        data = {"id": 77, "name": "Ivan Ivanov", "image_path": self.PLACEHOLDER}
        referee = get_or_create_referee(data)
        self.assertEqual(referee.photo_url, "")
        referee = get_or_create_referee({**data, "image_path": self.PHOTO})
        referee.refresh_from_db()
        self.assertEqual(referee.photo_url, self.PHOTO)


class CurrentSquadTests(TestCase):
    """Текущий состав из Sportmonks: применяется только свежий, переход виден до первого матча, старый матч не откатывает."""

    def setUp(self):
        from teams.models import Team

        self.old = Team.objects.create(name="Старый клуб", sportmonks_id="1")
        self.new = Team.objects.create(name="Новый клуб", sportmonks_id="2")

    def _rows(self, n, end="2030-06-30", first_id=500):
        return [{"player_id": first_id + i, "start": "2025-01-01", "end": end, "jersey_number": 10 + i,
                 "player": {"id": first_id + i, "display_name": f"Игрок {i}", "firstname": "Игрок", "lastname": f"Номер{i}"}}
                for i in range(n)]

    def test_fresh_squad_moves_player_and_stale_is_ignored(self):
        from unittest import mock
        from parsers.sportmonks.importers import sync_current_squad
        from players.models import Player

        moved = Player.objects.create(first_name="Игрок", last_name="Номер0", sportmonks_id="500", team=self.old,
                                      last_match_at=timezone.now() - timedelta(days=30))
        client = mock.Mock()
        client.get_team_squad.return_value = self._rows(3, end="2024-12-31")
        self.assertFalse(sync_current_squad(client, self.new)[0])  # устаревший — не трогаем
        moved.refresh_from_db()
        self.assertEqual(moved.team, self.old)

        client.get_team_squad.return_value = self._rows(12)
        ok, confirmed, _ = sync_current_squad(client, self.new)
        self.assertTrue(ok)
        self.assertEqual(confirmed, 12)
        moved.refresh_from_db()
        self.assertEqual((moved.team, moved.number), (self.new, 10))
        self.new.refresh_from_db()
        self.assertIsNotNone(self.new.squad_synced_at)

        # Переимпорт старого матча за прошлый клуб не возвращает игрока назад.
        from parsers.sportmonks.importers import get_or_create_player
        get_or_create_player({"id": 500, "display_name": "Игрок 0"}, team=self.old,
                             match_start_time=timezone.now() - timedelta(days=10))
        moved.refresh_from_db()
        self.assertEqual(moved.team, self.new)
        # А новый матч — обновляет как обычно.
        get_or_create_player({"id": 500, "display_name": "Игрок 0"}, team=self.old,
                             match_start_time=timezone.now() + timedelta(minutes=5))
        moved.refresh_from_db()
        self.assertEqual(moved.team, self.old)


class RefreshScheduleTests(TestCase):
    """Объявленное время и переносы подтягиваются из календаря на две недели вперёд."""

    def test_changed_kickoff_resynced(self):
        from datetime import datetime, timezone as dt_timezone
        from unittest import mock

        from evaluations.tests import _make_match
        from parsers.sportmonks import tasks

        match = _make_match(status="scheduled")
        match.sportmonks_id = "555"
        match.start_time = datetime(2026, 10, 10, 0, 0, tzinfo=dt_timezone.utc)
        match.save()
        fixtures = [{"id": 555, "starting_at": "2026-10-10 14:00:00"}]
        with mock.patch.object(tasks, "_sync_enabled", return_value=True), \
                mock.patch.object(tasks, "_get_league_and_season", return_value=(match.league, match.season)), \
                mock.patch.object(tasks, "SportmonksClient") as client, \
                mock.patch.object(tasks, "_heavy_sync_fixture", return_value=True) as heavy:
            client.return_value.get_fixtures_between.return_value = fixtures
            self.assertEqual(tasks.sportmonks_refresh_schedule.run(), 1)
            heavy.assert_called_once()
            match.start_time = datetime(2026, 10, 10, 14, 0, tzinfo=dt_timezone.utc)
            match.save()
            heavy.reset_mock()
            self.assertEqual(tasks.sportmonks_refresh_schedule.run(), 0)
            heavy.assert_not_called()


class LineupGapsFromEventsTests(TestCase):
    """Дыры в заявке у поставщика: автор гола и вышедший на замену дописываются; короткое имя не портит известное."""

    def setUp(self):
        self.league = _make_league()
        self.season = _make_season(self.league)

    def test_short_name_keeps_verified_name(self):
        get_or_create_player({"id": 555, "firstname": "Luka", "lastname": "Čermelj", "display_name": "Лука Чермель"})
        Player.objects.filter(sportmonks_id="555").update(first_name="Лука", last_name="Чермель", name_source="ai_verified")
        get_or_create_player({"id": 555, "name": "L. Kermelj"})
        p = Player.objects.get(sportmonks_id="555")
        self.assertEqual((p.first_name, p.last_name, p.name_source), ("Лука", "Чермель", "ai_verified"))

    def test_scorer_missing_from_lineup_is_added(self):
        from lineups.models import MatchLineupPlayer

        fixture = _fixture(sm_id=777010001, home_goals=1, away_goals=0)
        fixture["lineups"] = [{"id": 1, "team_id": 1001, "type_id": 11, "player_id": 901,
                               "player": {"id": 901, "display_name": "Иван Иванов"}, "jersey_number": 9}]
        fixture["events"] = [
            {"id": 50, "participant_id": 1001, "player_id": 902, "player_name": "P. Petrov", "minute": 70,
             "type": {"developer_name": "GOAL"}},
            {"id": 51, "participant_id": 1001, "player_id": 903, "player_name": "S. Sidorov", "minute": 60,
             "related_player_id": 901, "type": {"developer_name": "SUBSTITUTION"}},
        ]
        match = import_full_fixture(fixture, self.league, self.season)
        rows = {r.player.sportmonks_id: r for r in MatchLineupPlayer.objects.filter(lineup__match=match).select_related("player")}
        self.assertEqual(set(rows), {"901", "902", "903"})
        self.assertFalse(rows["902"].is_starting)
        self.assertEqual(rows["903"].minute_in, 60)


class KickoffChangeNoiseTests(SimpleTestCase):
    """Перенос сообщаем, только если он настоящий: заглушка «время не объявлено» и ночное время — не перенос."""

    def _pair(self, old, new):
        from types import SimpleNamespace
        return SimpleNamespace(status="scheduled", start_time=old), SimpleNamespace(status="scheduled", start_time=new)

    def _at(self, days, hour_utc):
        from datetime import datetime, timezone as dt_tz
        base = (timezone.now() + timedelta(days=days)).astimezone(dt_tz.utc)
        return datetime(base.year, base.month, base.day, hour_utc, 0, tzinfo=dt_tz.utc)

    def test_time_announced_same_day_is_silent(self):
        from parsers.sportmonks.importers import _detect_match_change
        self.assertIsNone(_detect_match_change(*self._pair(self._at(3, 0), self._at(3, 13))))
        self.assertIsNone(_detect_match_change(*self._pair(self._at(3, 13), self._at(3, 0))))

    def test_night_kickoff_is_silent(self):
        from parsers.sportmonks.importers import _detect_match_change
        self.assertIsNone(_detect_match_change(*self._pair(self._at(3, 12), self._at(3, 22))))  # 03:00 по Алматы

    def test_real_shift_is_reported(self):
        from parsers.sportmonks.importers import _detect_match_change
        self.assertEqual(_detect_match_change(*self._pair(self._at(3, 12), self._at(3, 11))), "rescheduled")

    def test_placeholder_formats_without_fake_time(self):
        from notifications.tasks import _fmt_kickoff
        self.assertIn("время не объявлено", _fmt_kickoff(self._at(3, 0)))


class EventTeamMixupTests(TestCase):
    """Команда события перепутана у поставщика — игрок не попадает в заявку соперника."""

    def test_player_of_other_team_not_added_to_wrong_lineup(self):
        from lineups.models import MatchLineupPlayer

        league = _make_league()
        season = _make_season(league)
        fixture = _fixture(sm_id=777040001, home_goals=0, away_goals=0)
        fixture["lineups"] = [{"id": 1, "team_id": 1001, "type_id": 11, "player_id": 901,
                               "player": {"id": 901, "display_name": "Милош Николич"}}]
        fixture["events"] = [{"id": 60, "participant_id": 1002, "player_id": 901, "player_name": "M. Nikolic",
                              "minute": 19, "type": {"developer_name": "YELLOWCARD"}}]
        match = import_full_fixture(fixture, league, season)
        sides = list(MatchLineupPlayer.objects.filter(lineup__match=match, player__sportmonks_id="901")
                     .values_list("lineup__side", flat=True))
        self.assertEqual(sides, ["home"])


class PlayerAliasTests(TestCase):
    """Один игрок под двумя id поставщика: событие под вторым id не плодит дубль, а пишет алиас."""

    def test_event_under_second_id_becomes_alias(self):
        from lineups.models import MatchLineupPlayer
        from players.models import PlayerSportmonksAlias

        league = _make_league()
        season = _make_season(league)
        fixture = _fixture(sm_id=777050001, home_goals=1, away_goals=0)
        fixture["lineups"] = [{"id": 1, "team_id": 1001, "type_id": 11, "player_id": 901,
                               "player": {"id": 901, "display_name": "Захар Гультяев"}}]
        fixture["events"] = [{"id": 70, "participant_id": 1001, "player_id": 999, "player_name": "Захар Гультяев",
                              "minute": 50, "type": {"developer_name": "GOAL"}}]
        match = import_full_fixture(fixture, league, season)
        self.assertEqual(MatchLineupPlayer.objects.filter(lineup__match=match).count(), 1)
        alias = PlayerSportmonksAlias.objects.get(sportmonks_id="999")
        self.assertEqual(alias.player.sportmonks_id, "901")
        self.assertEqual(match.events.get().player_id, alias.player_id)   # гол засчитан тому же игроку


class FixtureStateTests(TestCase):
    """Неизвестный статус не сбрасывает матч; прерванный матч не даёт результата; техрезультат без голосования."""

    def setUp(self):
        self.league = _make_league()
        self.season = _make_season(self.league)

    def test_unknown_state_keeps_status(self):
        match = import_full_fixture(_fixture(sm_id=777020001, dev_name="FT"), self.league, self.season)
        self.assertEqual(match.status, "finished")
        match = import_full_fixture(_fixture(sm_id=777020001, dev_name="AWAITING_UPDATES"), self.league, self.season)
        self.assertEqual(match.status, "finished")

    def test_interrupted_is_live_and_abandoned_has_no_result(self):
        self.assertEqual(import_full_fixture(_fixture(sm_id=777020002, dev_name="INTERRUPTED"), self.league, self.season).status, "live")
        self.assertEqual(import_full_fixture(_fixture(sm_id=777020003, dev_name="ABANDONED"), self.league, self.season).status, "postponed")

    def test_awarded_match_voting_closed(self):
        match = import_full_fixture(_fixture(sm_id=777020004, dev_name="AWARDED", starting_at=_recent_start()), self.league, self.season)
        self.assertTrue(match.decided_administratively)
        self.assertFalse(match.is_voting_open())


class SeasonRolloverTests(TestCase):
    """Календарь следующего сезона не пишется в текущий; в межсезонье новый сезон активируется заранее."""

    def setUp(self):
        self.league = _make_league()
        self.season = _make_season(self.league)

    def test_fixture_of_unknown_next_season_is_not_imported_into_current(self):
        fixture = _fixture(sm_id=777030001, dev_name="NS")
        fixture["season_id"] = 99999
        with self.assertRaises(ValueError):
            import_match_core(fixture, self.league, self.season)
        self.assertFalse(Match.objects.filter(sportmonks_id="777030001").exists())

    def test_fixture_goes_to_its_own_season(self):
        nxt = _make_season(self.league, year="2027", sportmonks_id="30000")
        fixture = _fixture(sm_id=777030002, dev_name="NS")
        fixture["season_id"] = 30000
        self.assertEqual(import_match_core(fixture, self.league, self.season).season, nxt)

    def test_preseason_resolution(self):
        from datetime import date
        from parsers.management.commands.sync_sportmonks_season import _resolve_current_season_id
        seasons = [{"id": 1, "starting_at": "2026-03-01", "ending_at": "2026-11-10"},
                   {"id": 2, "starting_at": "2027-03-06", "ending_at": "2027-11-10"}]
        self.assertEqual(_resolve_current_season_id(seasons, date(2026, 10, 10)), 1)
        self.assertEqual(_resolve_current_season_id(seasons, date(2026, 12, 15)), 1)   # до старта далеко
        self.assertEqual(_resolve_current_season_id(seasons, date(2027, 2, 1)), 2)     # за 33 дня — уже новый
