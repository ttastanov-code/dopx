# lineups/tests.py
"""
lineups хранит "сырые" данные состава ровно так, как их прислал KFF —
MatchLineupPlayer.position (амплуа GK/D/DM/M/AM/F) и .field_position
(сторона поля C/L/R/LC/RC) сознательно сделаны свободными CharField БЕЗ
choices и БЕЗ какой-либо нормализации на уровне модели/save() (см.
докстринг обоих полей в lineups/models.py и players/positions.py). Вся
нормализация регистра живёт СНАРУЖИ приложения lineups, в двух разных
местах:
  1. players/positions.py::clean_position_code() — вызывается вызывающим
     кодом на записи (parsers/kff/importers.py::import_lineups, и
     one-off бэкафиллом в lineups/migrations/0002_normalize_position_casing.py
     для уже накопленных до фикса данных).
  2. players/positions.py::resolve_lineup_codes() — комбинирует
     нормализованные position+field_position в код слота формации для
     season_squad/round_squad (см. многословный докстринг в самом модуле).

parsers/tests.py::ImportLineupsFormationTests уже покрывает сторону
ИМПОРТА (что null/отсутствующий `formation` не роняет IntegrityError).
Этот файл — про то, что происходит на уровне САМОЙ модели/домена lineups,
без парсера:
  - модель не падает на blank/пустых position и field_position (это не
    NULL — оба поля `blank=True`, `null` не объявлен, то есть Django сам
    подставляет "" при отсутствии значения — но эта деталь стоит того,
    чтобы её явно зафиксировать тестом, а не полагаться на память);
  - модель НЕ нормализует регистр сама по себе — это специально
    задокументированное разделение ответственности (clean_position_code
    вызывается на записи, а не в Model.save()), и здесь это проверяется
    явно, чтобы будущий рефакторинг случайно не понадеялся на
    "модель сама почистит";
  - сама нормализация (players/positions.py) корректно схлопывает разный
    регистр/пробелы для обоих полей и корректно комбинирует их в код
    слота — это и есть смысловое ядро "нормализации позиции", которое
    просила проверить задача, только физически лежит в соседнем модуле
    домена (players), а не в lineups/models.py, где нормализовать
    попросту нечему (там нет ни одного метода, кроме __str__).
  - unique-constraint на MatchLineup(match, team) и порядок сортировки
    MatchLineupPlayer (Meta.ordering) — то, что реально относится к
    lineups/models.py.
"""
from __future__ import annotations

from datetime import timedelta

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from leagues.models import League
from lineups.models import MatchLineup, MatchLineupPlayer
from matches.models import Match
from players.models import Player
from players.positions import clean_position_code, resolve_lineup_codes
from seasons.models import Season
from teams.models import Team


class LineupsTestCaseBase(TestCase):
    """Общий набор фикстур: лига/сезон/команды/матч — как в
    parsers/tests.py, чтобы конвенции создания объектов не расходились
    между тестами разных приложений одного домена."""

    def setUp(self):
        league = League.objects.create(name="Test League", country="KZ")
        season = Season.objects.create(league=league, year="2026")
        self.home_team = Team.objects.create(name="Home", external_id="100")
        self.away_team = Team.objects.create(name="Away", external_id="200")
        self.match = Match.objects.create(
            league=league, season=season,
            home_team=self.home_team, away_team=self.away_team,
            start_time=timezone.now(), voting_open_until=timezone.now() + timedelta(hours=48),
        )
        self.lineup = MatchLineup.objects.create(match=self.match, team=self.home_team, side="home")
        self.player = Player.objects.create(first_name="Test", last_name="Player", team=self.home_team)


class MatchLineupUniqueConstraintTests(LineupsTestCaseBase):
    """UniqueConstraint(fields=['match', 'team'], name='unique_match_team_lineup')
    — одна команда не может иметь два состава в одном матче (например,
    двойной вызов import_lineups из-за гонки двух воркеров, см. DistributedLockTests
    в parsers/tests.py — этот constraint вторая линия защиты на случай,
    если лок всё же не сработал)."""

    def test_duplicate_match_team_raises_integrity_error(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                MatchLineup.objects.create(match=self.match, team=self.home_team, side="home")

    def test_same_match_different_teams_is_allowed(self):
        # Не должно кидать — второй состав в том же матче, но другая команда.
        away_lineup = MatchLineup.objects.create(match=self.match, team=self.away_team, side="away")
        self.assertEqual(MatchLineup.objects.filter(match=self.match).count(), 2)
        self.assertNotEqual(away_lineup.pk, self.lineup.pk)


class MatchLineupPlayerFieldsTests(LineupsTestCaseBase):
    """position/field_position — blank CharField без null=True: Django
    сам подставляет "" при отсутствии значения, поэтому пустой/отсутствующий
    ввод не должен давать IntegrityError по NOT NULL, аналогично тому, как
    для `MatchLineup.formation` это уже проверено со стороны импорта
    (parsers/tests.py::ImportLineupsFormationTests)."""

    def test_create_without_position_or_field_position_does_not_raise(self):
        lp = MatchLineupPlayer.objects.create(lineup=self.lineup, player=self.player)
        lp.refresh_from_db()
        self.assertEqual(lp.position, "")
        self.assertEqual(lp.field_position, "")

    def test_create_with_explicit_empty_strings_does_not_raise(self):
        lp = MatchLineupPlayer.objects.create(
            lineup=self.lineup, player=self.player, position="", field_position="",
        )
        self.assertEqual(lp.position, "")
        self.assertEqual(lp.field_position, "")

    def test_model_does_not_normalize_casing_on_its_own(self):
        """Намеренное разделение ответственности (см. докстринг модуля
        выше и players/positions.py) — MatchLineupPlayer не переопределяет
        save()/clean() для регистра, в отличие от League.is_primary или
        Season.is_active, у которых save() ДЕЙСТВИТЕЛЬНО инкапсулирует
        свой инвариант. Здесь инвариант "единый регистр" сознательно
        оставлен на совести вызывающего кода (импортёра) — модель
        сохранит ровно то, что ей передали."""
        lp = MatchLineupPlayer.objects.create(
            lineup=self.lineup, player=self.player, position="gk", field_position="l",
        )
        lp.refresh_from_db()
        self.assertEqual(lp.position, "gk", "модель не нормализует регистр сама — это ответственность вызывающего кода")
        self.assertEqual(lp.field_position, "l")

    def test_ordering_is_substitutes_then_starters_by_shirt_number(self):
        """Meta.ordering = ['is_starting', 'shirt_number'] — по возрастанию
        булева поля False(0) идёт раньше True(1), то есть запасные
        оказываются ПЕРЕД стартовым составом при обходе без explicit
        order_by(). На первый взгляд контринтуитивно, но это тот же самый
        порядок, на который explicitly полагаются evaluations/views.py и
        evaluations/forms.py (`.order_by('is_starting', 'shirt_number')`)
        — то есть поведение согласовано между приложениями, здесь просто
        зафиксировано, чтобы будущий рефакторинг Meta.ordering не сломал
        оба потребителя молча."""
        starter = MatchLineupPlayer.objects.create(
            lineup=self.lineup, player=self.player, is_starting=True, shirt_number=9,
        )
        sub_player = Player.objects.create(first_name="Sub", last_name="Player", team=self.home_team)
        substitute = MatchLineupPlayer.objects.create(
            lineup=self.lineup, player=sub_player, is_starting=False, shirt_number=77,
        )

        ordered_ids = list(MatchLineupPlayer.objects.filter(lineup=self.lineup).values_list("pk", flat=True))
        self.assertEqual(ordered_ids, [substitute.pk, starter.pk])


class PositionNormalizationTests(TestCase):
    """players/positions.py::clean_position_code/resolve_lineup_codes —
    фактическое место, где "GK"/"gk"/"Gk" и т.п. схлопываются в единый
    вид, и где position+field_position комбинируются в код слота формации.
    Тестируется как чистые функции домена lineups (аргументы — сырые
    строки, ровно то, что лежит в MatchLineupPlayer.position/.field_position),
    без обращения к парсеру."""

    def test_clean_position_code_normalizes_case_and_whitespace(self):
        self.assertEqual(clean_position_code("gk"), "GK")
        self.assertEqual(clean_position_code("Gk"), "GK")
        self.assertEqual(clean_position_code("  GK  "), "GK")

    def test_clean_position_code_keeps_unknown_codes_instead_of_dropping(self):
        """Намеренно: неизвестный код KFF не должен теряться (см. докстринг
        модуля) — он всё равно попадёт в БД, просто в верхнем регистре."""
        self.assertEqual(clean_position_code("ss"), "SS")

    def test_clean_position_code_handles_empty_and_none(self):
        self.assertEqual(clean_position_code(""), "")
        self.assertEqual(clean_position_code(None), "")

    def test_resolve_lineup_codes_combines_amplua_and_zone_case_insensitively(self):
        self.assertEqual(resolve_lineup_codes("d", "l"), ["D:L"])
        self.assertEqual(resolve_lineup_codes("D", "L"), ["D:L"])
        self.assertEqual(resolve_lineup_codes(" d ", " L "), ["D:L"])

    def test_resolve_lineup_codes_folds_lc_and_rc_into_center_zone(self):
        """LC/RC (полу-фланги колонки формации) сворачиваются в общую "C" —
        отдельного слота "левый ЦЗ" в формации нет (см. докстринг
        _FIELD_POSITION_ZONE в players/positions.py)."""
        self.assertEqual(resolve_lineup_codes("D", "LC"), ["D:C"])
        self.assertEqual(resolve_lineup_codes("D", "RC"), ["D:C"])
        self.assertEqual(resolve_lineup_codes("D", "C"), ["D:C"])

    def test_resolve_lineup_codes_falls_back_to_bare_code_without_field_position(self):
        """Старые записи (синк до 2026-08-23) не имеют field_position —
        должны попадать в общий пул под голым кодом, а не отбрасываться."""
        self.assertEqual(resolve_lineup_codes("D", ""), ["D"])
        self.assertEqual(resolve_lineup_codes("D", None), ["D"])

    def test_resolve_lineup_codes_returns_empty_list_for_unrecognized_amplua(self):
        self.assertEqual(resolve_lineup_codes("", "L"), [])
        self.assertEqual(resolve_lineup_codes(None, "L"), [])
