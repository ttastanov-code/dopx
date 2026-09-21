# players/tests.py
"""
Тесты players/positions.py — чистые функции над строковыми кодами, БД не
нужна (SimpleTestCase).
"""
from __future__ import annotations

from django.test import SimpleTestCase

from players.positions import player_position_breakdown, player_position_display_code


class PlayerPositionDisplayCodeTests(SimpleTestCase):
    """2026-09-22, жалоба пользователя со скриншотом: сборная тура
    поставила игрока на правый фланг (через связку амплуа+field_position,
    см. players/positions.py::resolve_lineup_codes/SLOT_PROCESSING_ORDER),
    а на его собственном профиле мини-схема показывала ТОЛЬКО голое "M"/
    "F" — вообще без стороны. player_position_display_code() — та функция,
    которая должна была это чинить."""

    def test_ambiguous_amplua_with_known_side_becomes_side_specific(self):
        self.assertEqual(player_position_display_code("M", "R"), "RM")
        self.assertEqual(player_position_display_code("M", "L"), "LM")
        self.assertEqual(player_position_display_code("D", "R"), "RB")
        self.assertEqual(player_position_display_code("D", "L"), "LB")
        self.assertEqual(player_position_display_code("AM", "R"), "RW")
        self.assertEqual(player_position_display_code("F", "L"), "LW")

    def test_center_zone_keeps_bare_code(self):
        """Центральная зона (C) — не повод подменять код, игрок реально
        играл в центре."""
        self.assertEqual(player_position_display_code("M", "C"), "M")
        self.assertEqual(player_position_display_code("D", "C"), "D")

    def test_lc_rc_collapse_to_center_not_side(self):
        """LC/RC (край защитной тройки) схлопывается в "C" через тот же
        _FIELD_POSITION_ZONE, что использует squad-логика — центральный
        защитник из тройки не должен подписываться "левым защитником"
        только из-за небольшого смещения."""
        self.assertEqual(player_position_display_code("D", "LC"), "D")
        self.assertEqual(player_position_display_code("D", "RC"), "D")

    def test_missing_field_position_keeps_bare_code(self):
        """Старые записи без field_position (или невышедшие запасные) —
        как и раньше, просто голый код, ничего не подменяем без данных."""
        self.assertEqual(player_position_display_code("M", ""), "M")
        self.assertEqual(player_position_display_code("M", None), "M")

    def test_already_side_specific_amplua_is_not_touched(self):
        """RM/LB и т.п. уже однозначно сторонние сами по себе — подменять
        нечего, side_map для них просто не определена."""
        self.assertEqual(player_position_display_code("RM", "R"), "RM")
        self.assertEqual(player_position_display_code("LB", ""), "LB")

    def test_dm_is_not_side_shifted(self):
        """DM намеренно не входит в _ZONE_SIDE_EQUIVALENT — DM:L/DM:R в
        SLOT_PROCESSING_ORDER лишь техническое разделение CM1/CM2 в
        сборной, не реальная позиция на поле."""
        self.assertEqual(player_position_display_code("DM", "R"), "DM")
        self.assertEqual(player_position_display_code("DM", "L"), "DM")

    def test_unknown_amplua_returns_empty_string(self):
        self.assertEqual(player_position_display_code("", "R"), "")
        self.assertEqual(player_position_display_code(None, "R"), "")


class PlayerPositionBreakdownSideAwarenessTests(SimpleTestCase):
    """Контроль интеграции: если counts уже посчитаны с учётом стороны
    (как теперь делает players/views.py), боковой код должен нормально
    попасть в итоговую разбивку — с координатами/подписью правого
    полузащитника, а не потеряться."""

    def test_side_specific_code_appears_with_correct_label(self):
        rows = player_position_breakdown({"RM": 3, "F": 2})
        codes = {r["code"]: r for r in rows}
        self.assertIn("RM", codes)
        self.assertEqual(codes["RM"]["label"], "Правый полузащитник")
        self.assertEqual(codes["RM"]["count"], 3)
