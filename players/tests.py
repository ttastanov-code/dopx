# players/tests.py
"""Тесты players/positions.py (без БД)."""
from __future__ import annotations

from django.test import SimpleTestCase

from players.positions import player_position_breakdown, player_position_display_code


class PlayerPositionDisplayCodeTests(SimpleTestCase):
    """player_position_display_code учитывает сторону из field_position."""

    def test_ambiguous_amplua_with_known_side_becomes_side_specific(self):
        self.assertEqual(player_position_display_code("M", "R"), "RM")
        self.assertEqual(player_position_display_code("M", "L"), "LM")
        self.assertEqual(player_position_display_code("D", "R"), "RB")
        self.assertEqual(player_position_display_code("D", "L"), "LB")
        self.assertEqual(player_position_display_code("AM", "R"), "RW")
        self.assertEqual(player_position_display_code("F", "L"), "LW")

    def test_center_zone_keeps_bare_code(self):
        """C — код не меняется."""
        self.assertEqual(player_position_display_code("M", "C"), "M")
        self.assertEqual(player_position_display_code("D", "C"), "D")

    def test_lc_rc_collapse_to_center_not_side(self):
        """LC/RC -> C."""
        self.assertEqual(player_position_display_code("D", "LC"), "D")
        self.assertEqual(player_position_display_code("D", "RC"), "D")

    def test_missing_field_position_keeps_bare_code(self):
        """Без field_position — голый код."""
        self.assertEqual(player_position_display_code("M", ""), "M")
        self.assertEqual(player_position_display_code("M", None), "M")

    def test_already_side_specific_amplua_is_not_touched(self):
        """RM/LB уже с стороной — не меняем."""
        self.assertEqual(player_position_display_code("RM", "R"), "RM")
        self.assertEqual(player_position_display_code("LB", ""), "LB")

    def test_dm_is_not_side_shifted(self):
        """DM не подменяется."""
        self.assertEqual(player_position_display_code("DM", "R"), "DM")
        self.assertEqual(player_position_display_code("DM", "L"), "DM")

    def test_unknown_amplua_returns_empty_string(self):
        self.assertEqual(player_position_display_code("", "R"), "")
        self.assertEqual(player_position_display_code(None, "R"), "")


class PlayerPositionBreakdownSideAwarenessTests(SimpleTestCase):
    """Боковой код попадает в разбивку с координатами."""

    def test_side_specific_code_appears_with_correct_label(self):
        rows = player_position_breakdown({"RM": 3, "F": 2})
        codes = {r["code"]: r for r in rows}
        self.assertIn("RM", codes)
        self.assertEqual(codes["RM"]["label"], "Правый полузащитник")
        self.assertEqual(codes["RM"]["count"], 3)
