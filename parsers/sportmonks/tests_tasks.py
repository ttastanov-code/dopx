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

from parsers.sportmonks.importers import import_match_core
from parsers.sportmonks.tasks import sportmonks_update_live
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
