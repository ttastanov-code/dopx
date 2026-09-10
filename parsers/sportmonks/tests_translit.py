# parsers/sportmonks/tests_translit.py
"""
Регрессионные тесты на parsers/sportmonks/translit.py — раньше проверялся
только вручную (см. докстринг модуля "ПРОВЕРЕНО на примерах..."), без теста
эти примеры мог сломать любой будущий фикс, как чуть не случилось здесь.

ИСПРАВЛЕНО (2026-09-10, жалоба пользователя — судья "Григоры Московченко"
вместо "Григорий"): классические славянские имена на "-ий", записанные
ОДНИМ "y" без предшествующей "i" (Grigory, Yuriy/Yury, Vasily, Dmitry...),
транслитерировались неверно, давая "-ы" вместо "-ий".

ОТКАЧЕНО И ПЕРЕДЕЛАНО В ТОТ ЖЕ ДЕНЬ (жалоба "Кобыландий"/"Мухамбетжанулий"/
"Нуралий"/"Рустемулий"/"Серикулий" вместо "-ы"): первая версия фикса сделала
это ОБЩИМ правилом ("y" в конце слова после согласной -> "ий") — сломала
казахские имена/патроним "-ұлы" на "-ы", который в этом датасете (сборная
Казахстана) НАМНОГО чаще славянского "-ий". См. докстринг
_SINGLE_Y_FULL_NAME_OVERRIDES в translit.py — теперь точечный список
конкретных известных имён (whitelist полным словом), а не общее фонетическое
правило: чинит ИМЕННО Григория и подобных, не трогая вообще ничего другого.

Этот файл — чистый Python, БЕЗ Django-зависимостей (translit.py их не
использует) — можно гонять и `python3 -m unittest`, не только `manage.py
test`, но для единообразия с остальным проектом всё равно наследуется от
django.test.TestCase (пустая БД не мешает, тест её не трогает)."""
from __future__ import annotations

from django.test import SimpleTestCase

from parsers.sportmonks.translit import transliterate_name


class TransliterateNameRegressionTests(SimpleTestCase):
    """SimpleTestCase — без БД вообще, тест чисто функциональный."""

    def test_grigory_single_y_ending_transliterates_correctly(self):
        """ГЛАВНЫЙ РЕГРЕССИОННЫЙ ТЕСТ — точное воспроизведение жалобы."""
        self.assertEqual(transliterate_name("Grigory"), "Григорий")

    def test_grigoriy_double_i_ending_still_works(self):
        """Форма с двойным 'i' и раньше работала правильно (_WORD_FINAL_RULES)
        — контроль, что фикс её не сломал."""
        self.assertEqual(transliterate_name("Grigoriy"), "Григорий")

    def test_common_slavic_names_ending_in_single_y(self):
        """Тот же класс имён — все реально встречающиеся написания через
        одно 'y' (см. name_translations.py — многие тренеры/судьи КПЛ)."""
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
        """Rybakov -> Рыбаков — 'y' НЕ последняя буква слова, фикс не должен
        был это тронуть (см. докстринг фикса — граница изменения)."""
        self.assertEqual(transliterate_name("Rybakov"), "Рыбаков")

    def test_kazakh_names_ending_in_consonant_plus_y_are_not_broken_by_whitelist(self):
        """ГЛАВНЫЙ РЕГРЕССИОННЫЙ ТЕСТ #2 — точное воспроизведение второй
        жалобы того же дня: пять реальных имён, которые сломала ПЕРВАЯ
        версия фикса Григория (общее правило вместо whitelist). Казахский
        патроним "-ұлы"/"-uly" ("сын такого-то") и просто казахские имена на
        "-ы" должны остаться "-ы", не "-ий"."""
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
        """Toktybay -> Токтыбай — 'y' после гласной 'a' уже была "й" (другая
        ветка, не затронутая фиксом)."""
        self.assertEqual(transliterate_name("Toktybay"), "Токтыбай")
        self.assertEqual(transliterate_name("Sayat"), "Саят")

    def test_previously_verified_examples_still_pass(self):
        """См. докстринг модуля translit.py — "ПРОВЕРЕНО на примерах,
        которые пользователь явно указал как правильные" — эти два были
        только словесной гарантией без теста до сих пор."""
        self.assertEqual(transliterate_name("Stanislav Shcherbakov"), "Станислав Щербаков")
        self.assertEqual(transliterate_name("Magzhan Toktybay"), "Магжан Токтыбай")
