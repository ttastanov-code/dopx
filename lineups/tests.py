# lineups/tests.py
"""Тесты lineups: модель хранит сырые position/field_position без нормализации;
нормализация — в players/positions.py (clean_position_code, resolve_lineup_codes).
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
    """Общие фикстуры, как в parsers/tests.py."""

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
    """Один состав на команду в матче."""

    def test_duplicate_match_team_raises_integrity_error(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                MatchLineup.objects.create(match=self.match, team=self.home_team, side="home")

    def test_same_match_different_teams_is_allowed(self):
        # Другая команда в том же матче — ок.
        away_lineup = MatchLineup.objects.create(match=self.match, team=self.away_team, side="away")
        self.assertEqual(MatchLineup.objects.filter(match=self.match).count(), 2)
        self.assertNotEqual(away_lineup.pk, self.lineup.pk)


class MatchLineupPlayerFieldsTests(LineupsTestCaseBase):
    """Пустые position/field_position сохраняются как ""."""

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
        """Модель не меняет регистр — это задача импортёра."""
        lp = MatchLineupPlayer.objects.create(
            lineup=self.lineup, player=self.player, position="gk", field_position="l",
        )
        lp.refresh_from_db()
        self.assertEqual(lp.position, "gk", "модель не нормализует регистр сама — это ответственность вызывающего кода")
        self.assertEqual(lp.field_position, "l")

    def test_ordering_is_substitutes_then_starters_by_shirt_number(self):
        """Meta.ordering: запасные перед стартовыми — на это полагаются evaluations."""
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
    """clean_position_code / resolve_lineup_codes на сырых строках."""

    def test_clean_position_code_normalizes_case_and_whitespace(self):
        self.assertEqual(clean_position_code("gk"), "GK")
        self.assertEqual(clean_position_code("Gk"), "GK")
        self.assertEqual(clean_position_code("  GK  "), "GK")

    def test_clean_position_code_keeps_unknown_codes_instead_of_dropping(self):
        """Неизвестный код сохраняется (в верхнем регистре)."""
        self.assertEqual(clean_position_code("ss"), "SS")

    def test_clean_position_code_handles_empty_and_none(self):
        self.assertEqual(clean_position_code(""), "")
        self.assertEqual(clean_position_code(None), "")

    def test_resolve_lineup_codes_combines_amplua_and_zone_case_insensitively(self):
        self.assertEqual(resolve_lineup_codes("d", "l"), ["D:L"])
        self.assertEqual(resolve_lineup_codes("D", "L"), ["D:L"])
        self.assertEqual(resolve_lineup_codes(" d ", " L "), ["D:L"])

    def test_resolve_lineup_codes_folds_lc_and_rc_into_center_zone(self):
        """LC/RC -> C."""
        self.assertEqual(resolve_lineup_codes("D", "LC"), ["D:C"])
        self.assertEqual(resolve_lineup_codes("D", "RC"), ["D:C"])
        self.assertEqual(resolve_lineup_codes("D", "C"), ["D:C"])

    def test_resolve_lineup_codes_falls_back_to_bare_code_without_field_position(self):
        """Без field_position — голый код."""
        self.assertEqual(resolve_lineup_codes("D", ""), ["D"])
        self.assertEqual(resolve_lineup_codes("D", None), ["D"])

    def test_resolve_lineup_codes_returns_empty_list_for_unrecognized_amplua(self):
        self.assertEqual(resolve_lineup_codes("", "L"), [])
        self.assertEqual(resolve_lineup_codes(None, "L"), [])
