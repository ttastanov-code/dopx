# parsers/tests.py
"""
Регрессионные тесты на баги, реально найденные и исправленные в парсере
KFF за последнюю сессию рефакторинга. Смысл этого файла не в покрытии ради
цифры — каждый тест здесь закрывает КОНКРЕТНЫЙ инцидент, который уже
произошёл в проде хотя бы один раз:

  - ImportEventsAndMinutesTests.test_replace_existing_false_appends... —
    "живая лента событий замирала на первых ~50 минутах" (см. docstring
    `parsers/kff/importers.py::import_events_and_minutes`).
  - ImportLineupsFormationTests — "IntegrityError: null value in column
    formation" на КАЖДОМ цикле синка для матчей без объявленного состава.
  - DistributedLockTests — гонка двух воркеров на одном матче из-за
    задвоенного расписания в CELERY_BEAT_SCHEDULE (см. docstring
    `parsers/tasks.py`).

Без этих тестов следующий рефакторинг того же кода может тихо вернуть
любой из трёх багов обратно — уже бывало в этой сессии, что фикс одной
проблемы (например, дедупликация событий) требовал трогать тот же код,
где сидел formation-баг.
"""
from __future__ import annotations

import uuid
from datetime import timedelta

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from events.models import MatchEvent
from leagues.models import League
from lineups.models import MatchLineup
from matches.models import Match
from parsers.kff.importers import STATUS_MAP, import_events_and_minutes, import_lineups
from parsers.tasks import _acquire_match_sync_lock, _release_match_sync_lock
from seasons.models import Season
from teams.models import Team


class StatusMapTests(TestCase):
    """`STATUS_MAP` — единственное место, переводящее статус KFF в статус
    DOPX (`scheduled`/`live`/`finished`). Ошибка здесь тихо ломает и
    отображение матча, и voting_open_until, и алерты дашборда."""

    def test_known_statuses_map_correctly(self):
        self.assertEqual(STATUS_MAP["live"], "live")
        self.assertEqual(STATUS_MAP["finished"], "finished")
        self.assertEqual(STATUS_MAP["upcoming"], "scheduled")
        self.assertEqual(STATUS_MAP["postponed"], "scheduled")
        self.assertEqual(STATUS_MAP["cancelled"], "finished")

    def test_unknown_status_falls_back_to_current_status(self):
        """`parsers/tasks.py::update_match_statuses` вызывает
        `STATUS_MAP.get(api_status, match.status)` — неизвестный/новый
        статус от API НЕ должен затирать текущий статус матча на дефолт."""
        self.assertEqual(STATUS_MAP.get("some_new_kff_status", "live"), "live")


class ImportEventsAndMinutesTests(TestCase):
    """Регрессия: `replace_existing=False` (вызов из update_match_statuses
    с ДЕЛЬТОЙ событий) не должен трогать уже сохранённые события."""

    def setUp(self):
        league = League.objects.create(name="Test League", country="KZ")
        season = Season.objects.create(league=league, year="2026")
        self.home = Team.objects.create(name="Home", external_id="100")
        self.away = Team.objects.create(name="Away", external_id="200")
        self.match = Match.objects.create(
            league=league, season=season, home_team=self.home, away_team=self.away,
            start_time=timezone.now(), voting_open_until=timezone.now() + timedelta(hours=48),
        )

    @staticmethod
    def _event(minute, event_type="goal", team_id="100"):
        return {"minute": minute, "event_type": event_type, "team_id": team_id}

    def test_replace_existing_true_deletes_old_events(self):
        """Полный ресинк (pipeline.py::import_full_match) — старое поведение
        delete+recreate, корректно, когда `events` содержит ВЕСЬ список с API."""
        MatchEvent.objects.create(match=self.match, minute=10, event_type="goal", team_side="home")
        import_events_and_minutes(self.match, {"events": [self._event(20)]}, replace_existing=True)
        minutes = list(MatchEvent.objects.filter(match=self.match).values_list("minute", flat=True))
        self.assertEqual(minutes, [20])

    def test_replace_existing_false_appends_without_deleting_previous_cycles(self):
        """ИСПРАВЛЕННЫЙ баг: раньше delta-вызов из update_match_statuses на
        каждом цикле live-опроса стирал ВСЕ ранее сохранённые события и
        оставлял только последнюю дельту — лента "замирала" на событиях
        первых минут, хотя реальный счёт уходил на 90+'."""
        import_events_and_minutes(self.match, {"events": [self._event(10)]}, replace_existing=True)
        self.assertEqual(MatchEvent.objects.filter(match=self.match).count(), 1)

        import_events_and_minutes(self.match, {"events": [self._event(45)]}, replace_existing=False)
        minutes = sorted(MatchEvent.objects.filter(match=self.match).values_list("minute", flat=True))
        self.assertEqual(minutes, [10, 45], "оба события должны остаться, а не только последняя дельта")

        import_events_and_minutes(self.match, {"events": [self._event(88)]}, replace_existing=False)
        minutes = sorted(MatchEvent.objects.filter(match=self.match).values_list("minute", flat=True))
        self.assertEqual(minutes, [10, 45, 88], "третий цикл синка не должен стирать первые два")

    def test_team_side_determined_by_home_external_id(self):
        import_events_and_minutes(self.match, {"events": [self._event(30, team_id="100")]}, replace_existing=True)
        event = MatchEvent.objects.get(match=self.match)
        self.assertEqual(event.team_side, "home")

    def test_team_side_falls_back_to_away_for_unmatched_id(self):
        import_events_and_minutes(self.match, {"events": [self._event(30, team_id="200")]}, replace_existing=True)
        event = MatchEvent.objects.get(match=self.match)
        self.assertEqual(event.team_side, "away")

    def test_event_without_minute_is_skipped_not_crashed(self):
        """Защита от кривых данных API — событие без `minute` не должно
        валить всю функцию исключением (одно плохое событие не должно
        стоить всех остальных корректных событий того же цикла)."""
        result = import_events_and_minutes(
            self.match, {"events": [{"event_type": "goal"}]}, replace_existing=True
        )
        self.assertTrue(result)
        self.assertEqual(MatchEvent.objects.filter(match=self.match).count(), 0)

    def test_empty_events_list_returns_false(self):
        self.assertFalse(import_events_and_minutes(self.match, {"events": []}, replace_existing=True))
        self.assertFalse(import_events_and_minutes(self.match, {}, replace_existing=True))


class ImportLineupsFormationTests(TestCase):
    """Регрессия: `formation: null` от KFF (матч без объявленного состава)
    раньше валил IntegrityError на каждом цикле синка (formation — NOT NULL
    в БД, `.get("formation", "")` не подставляет дефолт, если ключ есть, но
    его значение — None)."""

    def setUp(self):
        league = League.objects.create(name="Test League", country="KZ")
        season = Season.objects.create(league=league, year="2026")
        self.home = Team.objects.create(name="Home", external_id="100")
        self.away = Team.objects.create(name="Away", external_id="200")
        self.match = Match.objects.create(
            league=league, season=season, home_team=self.home, away_team=self.away,
            start_time=timezone.now(), voting_open_until=timezone.now() + timedelta(hours=48),
        )

    def test_null_formation_does_not_raise_integrity_error(self):
        lineup_data = {
            "lineups": {
                "home_team": {"formation": None, "starters": [], "substitutes": []},
                "away_team": {"formation": None, "starters": [], "substitutes": []},
            }
        }
        # До фикса: IntegrityError: null value in column "formation" violates not-null constraint
        result = import_lineups(self.match, lineup_data)
        self.assertTrue(result)
        home_lineup = MatchLineup.objects.get(match=self.match, side="home")
        self.assertEqual(home_lineup.formation, "")

    def test_missing_formation_key_also_defaults_to_empty(self):
        """Отдельно от None — ключ вообще отсутствует в ответе API."""
        lineup_data = {
            "lineups": {
                "home_team": {"starters": [], "substitutes": []},
                "away_team": {"starters": [], "substitutes": []},
            }
        }
        import_lineups(self.match, lineup_data)
        home_lineup = MatchLineup.objects.get(match=self.match, side="home")
        self.assertEqual(home_lineup.formation, "")

    def test_real_formation_value_is_preserved(self):
        lineup_data = {
            "lineups": {
                "home_team": {"formation": "4-3-3", "starters": [], "substitutes": []},
                "away_team": {"formation": "4-4-2", "starters": [], "substitutes": []},
            }
        }
        import_lineups(self.match, lineup_data)
        self.assertEqual(MatchLineup.objects.get(match=self.match, side="home").formation, "4-3-3")
        self.assertEqual(MatchLineup.objects.get(match=self.match, side="away").formation, "4-4-2")


@override_settings(CACHES={
    "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"},
})
class DistributedLockTests(TestCase):
    """`cache.add()` — атомарный SETNX-лок на конкретный матч. Защищает от
    ситуации, задокументированной в `parsers/tasks.py`: одна и та же задача
    `update_match_statuses` зарегистрирована в CELERY_BEAT_SCHEDULE дважды
    под разными именами и может запуститься дважды почти одновременно."""

    def setUp(self):
        cache.clear()

    def test_first_acquire_succeeds(self):
        match_id = uuid.uuid4()
        self.assertTrue(_acquire_match_sync_lock(match_id, "worker-a"))

    def test_second_acquire_fails_while_lock_is_held(self):
        match_id = uuid.uuid4()
        self.assertTrue(_acquire_match_sync_lock(match_id, "worker-a"))
        self.assertFalse(_acquire_match_sync_lock(match_id, "worker-b"), "второй воркер не должен взять тот же лок")

    def test_locks_are_independent_per_match(self):
        """Лок на уровне МАТЧА, а не всей задачи целиком — воркер B должен
        свободно синхронизировать ДРУГОЙ матч, пока воркер A занят своим."""
        match_a, match_b = uuid.uuid4(), uuid.uuid4()
        self.assertTrue(_acquire_match_sync_lock(match_a, "worker-a"))
        self.assertTrue(_acquire_match_sync_lock(match_b, "worker-b"))

    def test_release_allows_reacquire(self):
        match_id = uuid.uuid4()
        self.assertTrue(_acquire_match_sync_lock(match_id, "worker-a"))
        _release_match_sync_lock(match_id)
        self.assertTrue(_acquire_match_sync_lock(match_id, "worker-b"))
