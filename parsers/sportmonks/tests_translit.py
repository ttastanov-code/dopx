# parsers/sportmonks/tests_translit.py
"""Тесты транслитерации (translit.py). Славянские имена на -y — через whitelist,
казахские -ы/-улы должны остаться -ы.
"""
from __future__ import annotations

from django.test import SimpleTestCase

from parsers.sportmonks.translit import transliterate_name


class TransliterateNameRegressionTests(SimpleTestCase):
    """Без БД."""

    def test_grigory_single_y_ending_transliterates_correctly(self):
        """Grigory -> Григорий."""
        self.assertEqual(transliterate_name("Grigory"), "Григорий")

    def test_grigoriy_double_i_ending_still_works(self):
        """Двойное i работало и раньше."""
        self.assertEqual(transliterate_name("Grigoriy"), "Григорий")

    def test_common_slavic_names_ending_in_single_y(self):
        """Другие славянские имена на одно y."""
        cases = {
            "Yuriy": "Юрий",
            "Yury": "Юрий",
            "Vasily": "Василий",
            "Anatoly": "Анатолий",
            "Vitaly": "Виталий",
            "Dmitry": "Дмитрий",
            "Arkady": "Аркадий",
            "Gennady": "Геннадий",
            "Valery": "Валерий",
        }
        for latin, expected in cases.items():
            with self.subTest(latin=latin):
                self.assertEqual(transliterate_name(latin), expected)

    def test_mid_word_y_after_consonant_unaffected(self):
        """y не в конце — не затронуто."""
        self.assertEqual(transliterate_name("Rybakov"), "Рыбаков")

    def test_kazakh_names_ending_in_consonant_plus_y_are_not_broken_by_whitelist(self):
        """Казахские имена и патроним -улы остаются на -ы."""
        cases = {
            "Kobylandy": "Кобыланды",
            "Nuraly": "Нуралы",
            "Mukhambetzhanuly": "Мухамбетжанулы",
            "Rustemuly": "Рустемулы",
            "Serikuly": "Серикулы",
        }
        for latin, expected in cases.items():
            with self.subTest(latin=latin):
                self.assertEqual(transliterate_name(latin), expected)

    def test_y_after_vowel_at_word_end_unaffected(self):
        """y после гласной -> й."""
        self.assertEqual(transliterate_name("Toktybay"), "Токтыбай")
        self.assertEqual(transliterate_name("Sayat"), "Саят")

    def test_previously_verified_examples_still_pass(self):
        """Примеры, указанные как правильные."""
        self.assertEqual(transliterate_name("Stanislav Shcherbakov"), "Станислав Щербаков")
        self.assertEqual(transliterate_name("Magzhan Toktybay"), "Магжан Токтыбай")
