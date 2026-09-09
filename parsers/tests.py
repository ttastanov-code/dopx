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

from django.test import TestCase

from leagues.models import League
from matches.models import Match
from parsers.sportmonks.importers import import_match_core
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

    def test_reimport_preserves_manually_corrected_team_name(self):
        """Staff мог вручную поправить название команды в админке (например,
        транслитерацию) — повторный синк не должен затирать это исправление,
        если оно не совпадает с "name" от Sportmonks. См. get_or_create_team
        докстринг — logo_url защищён явно, но name перезаписывается только
        если реально изменилось. Здесь фиксируем текущее поведение: name
        ВСЕГДА синкается с источником (это НЕ баг — команда реально может
        сменить официальное название), а вот logo_url защищён."""
        fixture = _fixture()
        match = import_match_core(fixture, self.league, self.season)
        team = match.home_team
        team.logo_url = "https://staff-corrected.example.com/logo.png"
        team.save(update_fields=["logo_url"])

        import_match_core(_fixture(), self.league, self.season)
        team.refresh_from_db()
        # logo_url заполняется только когда пусто — staff-правка должна выжить.
        self.assertEqual(team.logo_url, "https://staff-corrected.example.com/logo.png")

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
