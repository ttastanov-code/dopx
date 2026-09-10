# parsers/management/commands/tests_apply_cyrillic_names.py
"""
ИСПРАВЛЕНО (2026-09-10, реальный баг — "Григоры Московченко" вместо
"Григорий", хотя REFEREE_TRANSLATIONS[27929] уже содержал верное значение
задолго до жалобы): `_apply()` в apply_cyrillic_names.py проверяла "текущее
значение уже чистая кириллица?" ПЕРВОЙ, до того как вообще посмотреть в
словарь — "Григоры" полностью кириллическая строка (не смесь алфавитов),
поэтому проверка считала её "уже готовой" и словарь для этой записи НИКОГДА
не проверялся, никаким набором флагов. Тесты ниже — регрессия именно на
эту последовательность, не на работу словаря/транслитератора саму по себе
(это уже покрыто вручную выверенными данными в name_translations.py)."""
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
        """ГЛАВНЫЙ РЕГРЕССИОННЫЙ ТЕСТ: текущее значение — ПОЛНОСТЬЮ
        кириллическое (не смесь алфавитов), но неверное. Раньше это делало
        запись невидимой для словаря навсегда."""
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
        """Защита ручной правки staff / уже верного значения — НЕ в словаре
        и уже кириллица -> не трогаем (контрольная проверка, что фикс не
        снёс эту защиту вообще)."""
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
