# parsers/sportmonks/tests_tasks.py
"""
Регрессионный тест ИМЕННО на баг "матч Кайрат-Женис навсегда завис в
статусе live" (жалоба пользователя 2026-09-09) — см. докстринг фикса в
parsers/sportmonks/tasks.py::sportmonks_update_live.

ПОЧЕМУ ОТДЕЛЬНЫЙ ФАЙЛ (не parsers/tests.py): parsers/tests.py покрывает
import_match_core как чистую функцию с реальными fixture_data — здесь же
нужно мокать SportmonksClient (сетевой уровень), чтобы воспроизвести
ИМЕННО тот сценарий, который сломался в проде: "матч у нас live, но
Sportmonks его больше не отдаёт в /livescores/inplay". Без мока это
невозможно проверить без реального сыгранного матча КПЛ прямо сейчас —
а пользователь прямо указал, что ближайших матчей нет и ждать нечего.

ВАЖНО (та же оговорка, что в parsers/tests.py): в песочнице этой сессии
нет Django/сети, файл проверен только `python3 -m py_compile`. Запустите
`python manage.py test parsers.sportmonks.tests_tasks` на своей машине —
это и есть реальное подтверждение фикса, не мои слова.
"""
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
    """ИСПРАВЛЕНО (2026-09-10, реальный прод-инцидент — не гипотетический):
    воркер писал в лог на каждый тик Beat: "Received unregistered task of
    type 'parsers.sportmonks.tasks.sportmonks_update_live' ... The message
    has been ignored and discarded". КОРНЕВАЯ ПРИЧИНА — см. докстринг фикса
    в dopx/celery.py: `app.autodiscover_tasks()` без аргументов видит только
    `<app>.tasks` для приложений из INSTALLED_APPS; `parsers.sportmonks` —
    вложенный подпакет, а не отдельное приложение, поэтому НИ ОДНА из 6
    задач в parsers/sportmonks/tasks.py никогда не регистрировалась в
    процессе воркера — ни расписание (CELERY_BEAT_SCHEDULE), ни кнопки в
    staff-панели (dashboard/parser_tools.py::trigger_task, тоже через
    .delay()) не могли выполниться НИ РАЗУ. Логический фикс
    sportmonks_update_live (тесты выше) был бессмысленнен без этого —
    функция была правильной, но воркер её физически не мог вызвать.

    Это ПРЯМАЯ проверка того самого факта, который сломался в проде —
    что задача есть в реестре Celery-приложения, а не косвенное
    предположение через мок."""

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
    """Точное воспроизведение бага: матч у нас 'live', но выпал из ответа
    /livescores/inplay (потому что реально уже закончился)."""

    def setUp(self):
        self.league = _make_league()
        self.season = _make_season(self.league)
        # dev_name="INPLAY_2ND_HALF" -> STATE_MAP -> status='live' (тот же
        # реальный статус, в котором завис матч Кайрат-Женис в инциденте).
        fixture = _fixture(sm_id=555000111, dev_name="INPLAY_2ND_HALF")
        self.match = import_match_core(fixture, self.league, self.season)
        self.assertEqual(self.match.status, "live")  # sanity-check самого фикстура-хелпера

    @patch("parsers.sportmonks.tasks.SportmonksClient")
    def test_match_missing_from_livescores_gets_heavy_synced_to_finished(self, mock_client_cls):
        """ГЛАВНАЯ ПРОВЕРКА: sportmonks_update_live должна САМА заметить,
        что 'live'-матч больше не приходит в bulk-ответе, и досинхронизировать
        его тяжёлым вызовом — без этого фикса матч остался бы 'live' навсегда
        (именно это и произошло в проде 9 сентября)."""
        mock_client = MagicMock()
        mock_client.get_livescores.return_value = []  # матч реально закончился и пропал из inplay
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
        """Гарантия, что фикс не наступает на manual_override (staff
        сознательно заморозил статус — автосинк не должен его трогать,
        тот же принцип, что и везде в проекте)."""
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
        """Если матч ВСЁ ЕЩЁ есть в ответе Sportmonks (реально идёт) — не
        должно быть двойной обработки через оба пути (обычный цикл diff'а
        И блок реконсиляции одновременно)."""
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

        # Ровно один heavy sync (через обычный diff-путь по изменившемуся
        # счёту 2:1 -> 1:0), не два.
        self.assertEqual(mock_client.get_fixture.call_count, 1)


class SportmonksUpdateLiveEventSignatureTests(TestCase):
    """2026-09-21, жалоба пользователя со скриншотом (матч Астана-Кайрат):
    судья показал жёлтую, VAR пересмотрел и заменил на красную ТОМУ ЖЕ
    игроку — в ленте события остались ОБЕ карточки, будто было два разных
    нарушения, да ещё и с задержкой. См. полный разбор корневой причины в
    docstring sportmonks_update_live у сравнения `changed`.

    КОРНЕВАЯ ПРИЧИНА была ШИРЕ, чем просто дубль в БД (тот дубль отдельно
    чинится в parsers/sportmonks/importers.py::import_events через
    sportmonks_id, см. parsers/tests.py::test_var_card_upgrade_updates_
    same_event_no_duplicate) — лёгкий live-опрос (каждую минуту) решал,
    стоит ли ВООБЩЕ звать тяжёлую догрузку матча, ТОЛЬКО по изменению
    статуса/счёта. Карточка, замена, смена типа уже присланного события
    (VAR) не меняют ни то, ни другое — поэтому тяжёлая догрузка для них не
    вызывалась совсем, и правильный (уже исправленный на уровне БД)
    результат появлялся только случайно, на следующем голе или финальном
    свистке. Эти тесты проверяют именно это решение (`changed`), а не сам
    импорт события."""

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
        """Тот же матч, тот же счёт/статус, что уже в базе (иначе changed
        сработал бы и без сравнения событий, тест ничего бы не доказывал)
        — единственная переменная — developer_name события с id=1."""
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
        """ГЛАВНАЯ ПРОВЕРКА: id события тот же (1), но developer_name
        сменился YELLOWCARD → REDCARD (VAR) — статус/счёт матча НЕ
        изменились, но тяжёлая догрузка всё равно должна вызваться."""
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
        """Контрольная проверка: ничего не поменялось (тот же id, тот же
        developer_name), статус/счёт тоже не поменялись — тяжёлая
        догрузка НЕ должна вызываться (иначе фикс звонил бы каждый тик по
        каждому live-матчу без всякого смысла, сводя на нет саму идею
        двухуровневой схемы — см. докстринг модуля)."""
        mock_client = MagicMock()
        mock_client.get_livescores.return_value = [self._api_fixture("YELLOWCARD")]
        mock_client_cls.return_value = mock_client

        sportmonks_update_live()

        mock_client.get_fixture.assert_not_called()

    @patch("parsers.sportmonks.tasks.SportmonksClient")
    def test_new_event_id_triggers_heavy_sync(self, mock_client_cls):
        """Контрольная проверка на 'обычный' (не VAR) пропущенный случай —
        новая карточка/замена (новый id, которого раньше не было), которую
        до фикса тоже теряли, пока не поменяется счёт или статус."""
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
    """2026-09-13, реальный случай: занижённая статистика (3 удара против
    ~23 у стороннего источника) в завершённом матче Ордабасы-Астана —
    см. докстринг STATS_RESYNC_WINDOW в parsers/sportmonks/tasks.py за
    полным разбором корневой причины (sportmonks_update_live/
    sportmonks_sync_season синкают статистику ТОЛЬКО когда счёт/статус
    разошёлся — уже согласованный завершённый матч больше никогда не
    трогают, даже если статистика в нём объективно неполная)."""

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
        """ГЛАВНАЯ ПРОВЕРКА: heavy sync вызван, ХОТЯ счёт/статус между базой
        и ответом Sportmonks одинаковые (2:1 -> 2:1) — именно это отличает
        эту задачу от sportmonks_update_live/sportmonks_sync_season, у
        которых такой матч был бы молча пропущен как "без изменений"."""
        self._make_finished_match(sm_id=700000111, hours_ago=1)
        mock_client = MagicMock()
        mock_client.get_fixture.return_value = _fixture(
            sm_id=700000111, dev_name="FT", home_goals=2, away_goals=1,
        )
        mock_client_cls.return_value = mock_client

        sportmonks_resync_recent_stats()

        # Match.sportmonks_id — CharField (см. matches/models.py), поэтому
        # _heavy_sync_fixture передаёт сюда СТРОКУ ("700000111"), не int —
        # так же, как везде в проекте (import_match_core, sportmonks_update_
        # live и т.д. везде сравнивают/хранят sportmonks_id как str). Тест
        # раньше ошибочно ожидал int — код правильный, ожидание было неверным.
        mock_client.get_fixture.assert_called_once_with(
            "700000111", include=importers_module.HEAVY_FIXTURE_INCLUDE,
        )

    @patch("parsers.sportmonks.tasks.SportmonksClient")
    def test_match_older_than_window_is_not_touched(self, mock_client_cls):
        """Матч, завершившийся 4 часа назад (за пределами STATS_RESYNC_WINDOW
        = 3ч) — задача не должна дёргать API ради него бесконечно."""
        self._make_finished_match(sm_id=700000222, hours_ago=4)
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        sportmonks_resync_recent_stats()

        mock_client.get_fixture.assert_not_called()

    @patch("parsers.sportmonks.tasks.SportmonksClient")
    def test_scheduled_match_is_not_touched(self, mock_client_cls):
        """Ещё не сыгранный матч — не 'finished', задаче тут делать нечего."""
        fixture = _fixture(sm_id=700000333, dev_name="NS")
        match = import_match_core(fixture, self.league, self.season)
        self.assertEqual(match.status, "scheduled")
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        sportmonks_resync_recent_stats()

        mock_client.get_fixture.assert_not_called()
