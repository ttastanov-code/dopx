# parsers/management/commands/tests_apply_cyrillic_names.py
"""Тесты apply_cyrillic_names: запись из словаря применяется, даже если текущее имя
уже чистая кириллица.
"""
from __future__ import annotations

from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from referees.models import Referee


class ApplyCyrillicNamesDictPriorityTests(TestCase):
    def _run(self, *args):
        call_command("apply_cyrillic_names", *args)

    @patch("parsers.management.commands.apply_cyrillic_names.REFEREE_TRANSLATIONS", {12345: ("Григорий", "Московченко", "high")})
    def test_dict_entry_overrides_existing_clean_but_wrong_cyrillic(self):
        """Кириллическое, но неверное имя исправляется по словарю."""
        ref = Referee.objects.create(sportmonks_id="12345", first_name="Григоры", last_name="Московченко")
        self._run("--apply")
        ref.refresh_from_db()
        self.assertEqual(ref.first_name, "Григорий")
        self.assertEqual(ref.last_name, "Московченко")

    @patch("parsers.management.commands.apply_cyrillic_names.REFEREE_TRANSLATIONS", {12345: ("Григорий", "Московченко", "high")})
    def test_dry_run_is_default_and_does_not_write(self):
        ref = Referee.objects.create(sportmonks_id="12345", first_name="Григоры", last_name="Московченко")
        self._run()  # без --apply
        ref.refresh_from_db()
        self.assertEqual(ref.first_name, "Григоры")

    @patch("parsers.management.commands.apply_cyrillic_names.REFEREE_TRANSLATIONS", {})
    def test_record_not_in_dictionary_with_clean_cyrillic_is_left_alone(self):
        """Не в словаре и уже кириллица — не трогаем."""
        ref = Referee.objects.create(sportmonks_id="99999", first_name="Иван", last_name="Петров")
        self._run("--apply")
        ref.refresh_from_db()
        self.assertEqual(ref.first_name, "Иван")
        self.assertEqual(ref.last_name, "Петров")

    @patch(
        "parsers.management.commands.apply_cyrillic_names.REFEREE_TRANSLATIONS",
        {55555: ("Владимир", "Слишкович", "review")},
    )
    def test_review_confidence_entry_needs_explicit_include_review_flag(self):
        ref = Referee.objects.create(sportmonks_id="55555", first_name="Владимир", last_name="Слиskovic")
        self._run("--apply")  # без --include-review
        ref.refresh_from_db()
        self.assertEqual(ref.last_name, "Слиskovic")  # не тронуто

        self._run("--apply", "--include-review")
        ref.refresh_from_db()
        self.assertEqual(ref.last_name, "Слишкович")

    @patch("parsers.management.commands.apply_cyrillic_names.REFEREE_TRANSLATIONS", {12345: ("Григорий", "Московченко", "high")})
    def test_already_correct_dict_value_is_not_rewritten_needlessly(self):
        ref = Referee.objects.create(sportmonks_id="12345", first_name="Григорий", last_name="Московченко")
        self._run("--apply")
        ref.refresh_from_db()
        self.assertEqual(ref.first_name, "Григорий")
