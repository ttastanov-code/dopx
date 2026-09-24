# parsers/sportmonks/translit.py
"""Транслитерация латиница -> кириллица для казахских/русских/украинских имён.
Правила практической транскрипции (Shcherbakov -> Щербаков, Toktybay -> Токтыбай).
is_likely_foreign() — сигнал, что имя не славянское; влияет только на логирование.
"""
from __future__ import annotations

import re
from typing import List, Tuple

# Сочетания букв — длинные раньше коротких (shch, sch, sh, ch).
_MULTI_CHAR_RULES: List[Tuple[str, str]] = [
    ("shch", "щ"),
    ("sch", "щ"),
    ("zh", "ж"),
    ("kh", "х"),
    ("ts", "ц"),
    ("ch", "ч"),
    ("sh", "ш"),
    ("ph", "ф"),
    ("ya", "я"),
    ("yu", "ю"),
    ("yo", "ё"),
    ("ye", "е"),
]

# ay/ey/oy/uy не диграфы — иначе ломается «Sayat» -> «Саят». Одиночная y обрабатывается ниже.
# iy/yi/yy — только на конце слова (-ий/-ый).
_WORD_FINAL_RULES: List[Tuple[str, str]] = [
    ("iy", "ий"),
    ("yi", "ий"),
    ("yy", "ый"),
]

_SINGLE_CHAR_MAP = {
    "a": "а", "b": "б", "v": "в", "g": "г", "d": "д", "e": "е", "z": "з",
    "i": "и", "k": "к", "l": "л", "m": "м", "n": "н", "o": "о", "p": "п",
    "r": "р", "s": "с", "t": "т", "u": "у", "f": "ф", "h": "х", "c": "к",
    "w": "в", "j": "й", "q": "к",
    # x -> кс.
    "x": "кс",
}

_VOWELS_RU = set("аеёиоуыэюя")

# Признаки не славянского имени.
_FOREIGN_SIGNAL_RE = re.compile(
    r"[àáâãäåæçèéêëìíîïñòóôõöùúûüýÿ]"  # диакритика романских языков
    r"|[ćčđšžł]"  # сербско-хорватские/польские буквы
    r"|lh|nh|ção|ção|inho|inha"  # португальские паттерны
    r"|[wq]",  # редкие в славянской латинице
    re.IGNORECASE,
)


def is_likely_foreign(latin_text: str) -> bool:
    """Имя выглядит не славянским. Только для логирования."""
    return bool(_FOREIGN_SIGNAL_RE.search(latin_text or ""))


# Славянские имена на одиночную -y (Grigory -> Григорий).
# Только полное совпадение слова — общее правило ломает казахские -ы/-улы.
_SINGLE_Y_FULL_NAME_OVERRIDES = {
    "grigory": "григорий",
    "yuriy": "юрий",
    "yury": "юрий",
    "vasily": "василий",
    "anatoly": "анатолий",
    "vitaly": "виталий",
    "dmitry": "дмитрий",
    "arkady": "аркадий",
    "gennady": "геннадий",
    "valery": "валерий",
}


def _transliterate_word(word: str) -> str:
    suffix_cyr = ""
    for latin_suffix, cyr_suffix in _WORD_FINAL_RULES:
        if word.endswith(latin_suffix) and len(word) > len(latin_suffix):
            word, suffix_cyr = word[: -len(latin_suffix)], cyr_suffix
            break

    result = []
    i = 0
    n = len(word)
    while i < n:
        matched = False
        for pattern, repl in _MULTI_CHAR_RULES:
            if word.startswith(pattern, i):
                result.append(repl)
                i += len(pattern)
                matched = True
                break
        if matched:
            continue

        ch = word[i]
        if ch == "y":
            # y после гласной -> «й», иначе -> «ы».
            prev = result[-1] if result else ""
            result.append("й" if prev in _VOWELS_RU else "ы")
            i += 1
            continue

        result.append(_SINGLE_CHAR_MAP.get(ch, ch))  # неизвестный символ — как есть
        i += 1

    return "".join(result) + suffix_cyr


def transliterate_name(latin_text: str) -> str:
    """Транслитерирует ФИО с заглавной первой буквой каждого слова."""
    if not latin_text:
        return ""

    def _translit_token(token: str) -> str:
        if not token:
            return token
        lowered = token.lower()
        # Полное совпадение токена со списком исключений.
        override = _SINGLE_Y_FULL_NAME_OVERRIDES.get(lowered)
        cyr = override if override is not None else _transliterate_word(lowered)
        return cyr[:1].upper() + cyr[1:] if cyr else cyr

    # Делим по пробелам; части через дефис — тоже с заглавной.
    words = latin_text.split(" ")
    out_words = []
    for word in words:
        if "-" in word:
            parts = word.split("-")
            out_words.append("-".join(_translit_token(p) for p in parts))
        else:
            out_words.append(_translit_token(word))
    return " ".join(out_words)
