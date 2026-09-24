# parsers/tests.py
"""Тесты импортёра Sportmonks (parsers/sportmonks/importers.py)."""
from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase

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
        def _fake_import_lineups(match, lineups_data, formations_data=None):
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
        def _fake_import_lineups(match, lineups_data, formations_data=None):
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
        def _fake_import_lineups(match, lineups_data, formations_data=None):
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

    def test_abandoned_state_sets_flag(self):
        match = import_match_core(_fixture(dev_name="ABANDONED"), self.league, self.season)
        self.assertTrue(match.decided_administratively)

    def test_normal_finished_match_does_not_set_flag(self):
        match = import_match_core(_fixture(dev_name="FT"), self.league, self.season)
        self.assertFalse(match.decided_administratively)

    @patch("parsers.tasks._send_sync_error_alert")
    def test_alert_excludes_walkover_matches_without_lineup(self, mock_alert):
        """6 технических матчей без состава — алерта нет."""
        for i in range(6):
            match = import_match_core(_fixture(sm_id=800000000 + i, dev_name="WO"), self.league, self.season)
            self.assertFalse(match.has_lineup)

        result = check_sync_errors_and_alert()
        self.assertEqual(result, {"status": "ok"})
        mock_alert.assert_not_called()

    @patch("parsers.tasks._send_sync_error_alert")
    def test_alert_still_fires_for_genuine_missing_lineups(self, mock_alert):
        """Контроль: обычные матчи без состава — алерт есть."""
        for i in range(6):
            import_match_core(_fixture(sm_id=810000000 + i, dev_name="FT"), self.league, self.season)

        result = check_sync_errors_and_alert()
        self.assertEqual(result["status"], "alert_sent")
        mock_alert.assert_called_once()


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
