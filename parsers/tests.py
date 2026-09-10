# parsers/tests.py
"""
Автотесты для Sportmonks-импортёра (parsers/sportmonks/importers.py).

Файл был удалён в более ранней сессии вместе со старым KFF-импортёром и с
тех пор отсутствовал — пункт P1 из код-ревью 2026-09-09 ("нет полноценного
автоматического тестового набора"). Создан заново с нуля, покрывает
конкретные риски, которые реально всплывали в этом проекте (см. ссылки в
докстринге каждого теста), а не абстрактный чек-лист.

ВАЖНО: в песочнице разработки этой сессии нет сетевого доступа к PyPI
(подтверждено: `pip install -r requirements.txt` падает с ProxyError/403),
поэтому здесь Django install отсутствует и `manage.py test` в сандбоксе
запустить нельзя. Этот файл проверен только на синтаксическую корректность
(`python3 -m py_compile parsers/tests.py`). Запустите
`python manage.py test parsers` на своей машине, чтобы реально исполнить
тесты.
"""
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
    """Минимальный, но реалистичный fixture_data — форма подтверждена вживую
    2026-09-08 прямым запросом к GET /fixtures/{id} (см. докстринг модуля
    parsers/sportmonks/importers.py), не придумана."""
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


class ImportMatchCoreTests(TestCase):
    """import_match_core: создание, идемпотентность повторного импорта,
    manual_override, и P0-защита от чужой лиги (Codex-ревью 2026-09-09)."""

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
        # end_time/voting_open_until должны выставляться для finished-матча
        self.assertIsNotNone(match.end_time)
        self.assertIsNotNone(match.voting_open_until)

    def test_reimport_is_idempotent_no_duplicate_matches(self):
        fixture = _fixture()
        first = import_match_core(fixture, self.league, self.season)
        second = import_match_core(fixture, self.league, self.season)

        self.assertEqual(first.id, second.id)
        self.assertEqual(Match.objects.filter(sportmonks_id="19681993").count(), 1)
        # Команды тоже не должны дублироваться при повторном импорте.
        self.assertEqual(Team.objects.filter(sportmonks_id="1001").count(), 1)
        self.assertEqual(Team.objects.filter(sportmonks_id="1002").count(), 1)

    def test_reimport_syncs_logo_url_freely(self):
        """ОБНОВЛЕНО (2026-09-09, вопрос пользователя "менеджер сказал, что
        логотипы обновят за 24 часа, но мы вроде сделали так, чтобы не
        обновлялись — как быть, если я сам захочу загрузить лого?"):
        раньше logo_url писался только один раз (если было пусто) — из-за
        этого НИКОГДА не подхватывались настоящие обновления логотипа от
        Sportmonks, что и было жалобой. Теперь logo_url ВСЕГДА синкается
        свежим значением с источника — тест ниже фиксирует именно это
        (старая версия этого теста проверяла противоположное и была
        переписана вместе с самим поведением, не по ошибке отдельно).
        Staff-защита переехала на ДРУГОЕ поле — см. следующий тест."""
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
        """Ручная защита теперь — файл `Team.logo` (загруженный в админке),
        НЕ строковый `logo_url` (см. тест выше и teams/models.py::
        Team.logo_display докстринг). `logo_display` всегда предпочитает
        загруженный файл over logo_url, а синк из Sportmonks вообще не
        трогает поле `logo` — так что оно переживает любое число
        повторных импортов независимо от того, что присылает источник."""
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

        # Источник теперь говорит "матч сыгран" — но manual_override должен
        # заблокировать перезапись статуса/даты (тот же guard, что у KFF).
        updated_fixture = _fixture(dev_name="FT", starting_at="2026-09-01 10:00:00")
        result = import_match_core(updated_fixture, self.league, self.season)

        self.assertEqual(result.id, match.id)
        self.assertEqual(result.status, "postponed")
        self.assertEqual(result.start_time, original_start)

    def test_foreign_league_fixture_is_rejected(self):
        """P0 (Codex-ревью 2026-09-09): fixture с league_id, не совпадающим
        с ожидаемой лигой, должен быть отклонён с ValueError — это третий,
        последний барьер защиты от записи чужой лиги под видом КПЛ (первые
        два — client.py::get_livescores фильтр-параметр и explicit-проверка
        в parsers/sportmonks/tasks.py::sportmonks_update_live)."""
        foreign_fixture = _fixture(league_id=999)
        with self.assertRaises(ValueError):
            import_match_core(foreign_fixture, self.league, self.season)
        self.assertFalse(Match.objects.filter(sportmonks_id="19681993").exists())

    def test_missing_league_id_in_fixture_does_not_raise(self):
        """Не все include гарантируют поле league_id — его отсутствие не
        должно считаться ошибкой (два других барьера всё ещё в строю)."""
        fixture = _fixture()
        del fixture["league_id"]
        match = import_match_core(fixture, self.league, self.season)
        self.assertEqual(match.league_id, self.league.id)

    def test_unknown_state_defaults_to_scheduled_without_crashing(self):
        """Неизвестный developer_name (например, новый статус, который
        Sportmonks добавит позже и который ещё не попал в STATE_MAP) не
        должен ронять импорт — только залогировать warning и по умолчанию
        считать матч 'scheduled' (тот же паттерн диагностики, что STATUS_MAP
        у KFF-импортёра)."""
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


def _goal_event(minute: int = 87, participant_id: int = 1001, dev_name: str = "GOAL") -> dict:
    """Минимальная форма события Sportmonks (см. EVENT_DEV_NAME_MAP,
    подтверждено вживую — см. докстринг модуля importers.py)."""
    return {
        "type": {"developer_name": dev_name},
        "minute": minute,
        "participant_id": participant_id,
        "player_id": None,
        "related_player_id": None,
        "result": "1-0",
        "extra_minute": 0,
    }


class ImportFullFixtureNotificationWiringTests(TestCase):
    """ИСПРАВЛЕНО (2026-09-10, жалоба "не работают пушы по событиям!!!"):
    regression-тест ИМЕННО на факт вызова — не на то, что сами задачи
    notify_followers_match_activity/notify_followers_match_event правильно
    формируют уведомления (это уже покрыто notifications/tests.py), а на то,
    что import_full_fixture их РЕАЛЬНО СТАВИТ В ОЧЕРЕДЬ. Баг был именно в
    этом: обе задачи были рабочими и протестированными по отдельности, но
    их никто не вызывал — см. полный разбор в докстринге import_events и
    import_full_fixture (parsers/sportmonks/importers.py).

    django.test.TestCase.captureOnCommitCallbacks(execute=True) — без этого
    transaction.on_commit(...) внутри теста никогда бы не выполнился (тест
    сам обёрнут в незакоммиченную транзакцию с rollback в конце) и тест бы
    "зеленел", даже если постановка в очередь физически отсутствует —
    ложноположительный тест хуже отсутствующего, поэтому это не опционально."""

    def setUp(self):
        self.league = _make_league()
        self.season = _make_season(self.league)

    @patch("notifications.tasks.notify_followers_match_activity.delay")
    def test_first_transition_to_finished_queues_activity_notification(self, mock_delay):
        fixture = _fixture(sm_id=777000111, dev_name="INPLAY_2ND_HALF")
        match = import_full_fixture(fixture, self.league, self.season)
        self.assertEqual(match.status, "live")
        mock_delay.assert_not_called()

        finished_fixture = _fixture(sm_id=777000111, dev_name="FT")
        with self.captureOnCommitCallbacks(execute=True):
            match = import_full_fixture(finished_fixture, self.league, self.season)

        self.assertEqual(match.status, "finished")
        mock_delay.assert_called_once_with(str(match.id))

    @patch("notifications.tasks.notify_followers_match_activity.delay")
    def test_reimporting_already_finished_match_does_not_requeue(self, mock_delay):
        """Досинхронизация уже завершённого матча (например, догрузка
        статистики отдельным тяжёлым вызовом после финального свистка) НЕ
        должна слать повторное приглашение оценить матч."""
        fixture = _fixture(sm_id=777000222, dev_name="FT")
        with self.captureOnCommitCallbacks(execute=True):
            import_full_fixture(fixture, self.league, self.season)
        self.assertEqual(mock_delay.call_count, 1)

        with self.captureOnCommitCallbacks(execute=True):
            import_full_fixture(_fixture(sm_id=777000222, dev_name="FT"), self.league, self.season)
        self.assertEqual(mock_delay.call_count, 1)

    @patch("notifications.tasks.notify_followers_match_event.delay")
    def test_new_goal_event_queues_push_worthy_notification(self, mock_delay):
        # dev_name='INPLAY_2ND_HALF' (матч ещё ИДЁТ, не 'FT') — намеренно:
        # события/голы происходят ПО ХОДУ игры, а не только к финальному
        # свистку, и это же исключает побочное срабатывание проверки
        # "match.status == finished" (activity-уведомление) в этом тесте —
        # она проверяется отдельно выше, тестам на события тут делать нечего.
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
        """Жёлтая карточка — реальное, сохраняемое событие, но НЕ входит в
        PUSH_WORTHY_EVENT_TYPES (см. notifications/tasks.py) — гол/автогол/
        пенальти/отменённый гол/красная карточка только. Живой пуш на каждую
        жёлтую карточку был бы шумом, не сигналом."""
        fixture = _fixture(sm_id=777000444, dev_name="INPLAY_2ND_HALF")
        fixture["events"] = [_goal_event(minute=54, participant_id=fixture["participants"][0]["id"], dev_name="YELLOWCARD")]

        with self.captureOnCommitCallbacks(execute=True):
            import_full_fixture(fixture, self.league, self.season)

        mock_delay.assert_not_called()

    @patch("notifications.tasks.notify_followers_match_event.delay")
    def test_reimporting_same_event_does_not_requeue(self, mock_delay):
        """import_events обновляет уже существующее событие НА МЕСТЕ
        (см. её докстринг про идемпотентность) — повторный импорт того же
        гола не должен слать пуш во второй раз."""
        fixture = _fixture(sm_id=777000555, dev_name="INPLAY_2ND_HALF")
        fixture["events"] = [_goal_event(minute=23, participant_id=fixture["participants"][0]["id"])]

        with self.captureOnCommitCallbacks(execute=True):
            import_full_fixture(fixture, self.league, self.season)
        self.assertEqual(mock_delay.call_count, 1)

        with self.captureOnCommitCallbacks(execute=True):
            import_full_fixture(fixture, self.league, self.season)
        self.assertEqual(mock_delay.call_count, 1)


class DecidedAdministrativelyTests(TestCase):
    """ИСПРАВЛЕНО (2026-09-10, расследование алерта "12 матчей без составов
    за 24ч"): матчи, завершённые техническим решением (неявка/техническое
    поражение/прерван и засчитан), законно не имеют lineups/events — не
    должны считаться ошибкой синхронизации. См. docstring Match.
    decided_administratively (matches/models.py) и правку в parsers/tasks.py
    ::check_sync_errors_and_alert."""

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
        """6 обычных finished-матчей без состава ПРЕВЫШАЮТ порог (5, см.
        check_sync_errors_and_alert) — но если это технические поражения,
        алерт не должен сработать вообще."""
        for i in range(6):
            match = import_match_core(_fixture(sm_id=800000000 + i, dev_name="WO"), self.league, self.season)
            self.assertFalse(match.has_lineup)

        result = check_sync_errors_and_alert()
        self.assertEqual(result, {"status": "ok"})
        mock_alert.assert_not_called()

    @patch("parsers.tasks._send_sync_error_alert")
    def test_alert_still_fires_for_genuine_missing_lineups(self, mock_alert):
        """Контрольная проверка: та же ситуация, но с НЕадминистративными
        finished-матчами без состава — алерт должен сработать как раньше,
        фикс не должен был случайно заглушить реальные сбои синка."""
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
    """ИСПРАВЛЕНО ВТОРОЙ РАЗ (2026-09-10, жалоба "у нас всё ещё Эркин
    Тапалов вместо Еркин" — уже ПОСЛЕ того, как fix_known_wrong_names
    --apply отчитался об успешном исправлении именно этой записи в базе).
    КОРЕНЬ: первая версия PLAYER_NAME_CORRECTIONS была ключована по сырому
    ЛАТИНСКОМУ firstname/lastname, но Sportmonks для этих игроков шлёт
    ГОТОВУЮ КИРИЛЛИЦУ прямо в firstname/lastname — сверка с латинскими
    ключами никогда не совпадала, поправка молча не срабатывала на
    импорте, и разовое исправление в базе откатывалось первым же
    следующим синком (get_or_create_player обновляет имя КАЖДЫЙ раз, см.
    её докстринг). Тесты ниже воспроизводят ИМЕННО этот сценарий — не
    "правильно ли считает транслитерация", а "переживает ли исправленное
    имя повторный импорт с теми же неверными сырыми данными от источника"."""

    def test_cyrillic_wrong_name_from_sportmonks_firstname_lastname_is_corrected(self):
        """ГЛАВНЫЙ РЕГРЕССИОННЫЙ ТЕСТ: Sportmonks шлёт ГОТОВУЮ (неверную)
        кириллицу прямо в firstname/lastname (не латиницу) — ровно так, как
        оказалось на реальном матче с Тапаловым."""
        player_data = _sportmonks_player(9001001, firstname="Эркин", lastname="Тапалов")
        player = get_or_create_player(player_data)
        self.assertEqual(player.first_name, "Еркин")
        self.assertEqual(player.last_name, "Тапалов")

    def test_correction_survives_reimport_with_same_wrong_raw_data(self):
        """ГЛАВНАЯ ПРОВЕРКА "не откатывается на следующем синке" — повторный
        импорт с ТЕМИ ЖЕ сырыми (неверными) данными от источника не должен
        отменить исправление. Раньше именно это и происходило."""
        player_data = _sportmonks_player(9001002, firstname="Эркин", lastname="Тапалов")
        get_or_create_player(player_data)

        # Второй "синк" — Sportmonks по-прежнему присылает то же самое
        # неверное "Эркин" (источник не поменялся, поправка — только у нас).
        player = get_or_create_player(_sportmonks_player(9001002, firstname="Эркин", lastname="Тапалов"))
        self.assertEqual(player.first_name, "Еркин")

        self.assertEqual(Player.objects.filter(sportmonks_id="9001002").count(), 1)

    def test_manually_corrected_db_record_is_not_reverted_by_next_sync(self):
        """Симуляция ТОЧНОЙ последовательности инцидента: 1) в базе уже
        лежит исправленное вручную имя (как после fix_known_wrong_names
        --apply), 2) прилетает обычный синк с сырыми данными, которые ДО
        фикса откатывали имя назад."""
        Player.objects.create(sportmonks_id="9001003", first_name="Еркин", last_name="Тапалов")
        player = get_or_create_player(_sportmonks_player(9001003, firstname="Эркин", lastname="Тапалов"))
        self.assertEqual(player.first_name, "Еркин")

    def test_transliteration_typo_is_also_corrected_via_same_mechanism(self):
        """"Rafael" транслитерируется алгоритмом в "Рафаел" (без смягчения
        на конце, известный пробел в translit.py) — поправка теперь сверяет
        РЕЗУЛЬТАТ, а не сырой источник, поэтому ловит и этот случай тем же
        словарём, без отдельной латинской ветки."""
        player = get_or_create_player(_sportmonks_player(9001004, firstname="Rafael", lastname="Testov"))
        self.assertEqual(player.first_name, "Рафаэль")

    def test_unrelated_slavic_name_is_not_affected(self):
        """Контрольная проверка: "Pavel" -> "Павел" получается тем же
        транслитератором и НЕ должен задеваться поправкой (её вообще нет в
        словаре как ключа) — иначе легко было бы случайно "смягчить" славянское
        имя тем же правилом, что и заимствованное "Rafael"."""
        player = get_or_create_player(_sportmonks_player(9001005, firstname="Pavel", lastname="Testov"))
        self.assertEqual(player.first_name, "Павел")
