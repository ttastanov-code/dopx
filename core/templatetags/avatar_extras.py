# core/templatetags/avatar_extras.py
"""
Генеративный аватар вместо фото (2026-08-21): после отказа от
автоматического импорта фотографий игроков с kffleague.kz (нестабильный
источник — рассинхрон составов, кривой авто-кроп/компоновка лица в круге,
см. историю правок season_squad/photo_processing.py и
parsers/kff/photo_scraper.py в этой сессии) — вместо фото везде, где его
нет, показываем собственный узнаваемый аватар: детерминированный
градиент + инициалы, без внешних зависимостей и без единой точки отказа
на стороннем сайте (тот же принцип, что у дефолтных аватарок GitHub/Slack).

Photo как таковое НЕ удалено из модели/шаблонов — если staff вручную
загрузит фото игрока через админку, оно по-прежнему покажется (см.
players/admin.py, поле photo редактируемо). Просто больше ничего не
скачивает и не обрабатывает фото автоматически.
"""
from __future__ import annotations

import hashlib

from django import template

register = template.Library()

# Фиксированная палитра, согласованная с daisyUI-темой сайта (primary/
# success/info/warning/error и их соседи по кругу) — так все генеративные
# аватары выглядят частью одного визуального языка, а не случайным цветовым
# шумом от произвольного HSL по хэшу.
_GRADIENTS = [
    "linear-gradient(135deg, #6366f1, #4338ca)",  # indigo (primary)
    "linear-gradient(135deg, #06b6d4, #0e7490)",  # cyan
    "linear-gradient(135deg, #10b981, #047857)",  # emerald (success)
    "linear-gradient(135deg, #f59e0b, #b45309)",  # amber (warning)
    "linear-gradient(135deg, #ef4444, #b91c1c)",  # red (error)
    "linear-gradient(135deg, #8b5cf6, #6d28d9)",  # violet
    "linear-gradient(135deg, #ec4899, #be185d)",  # pink
    "linear-gradient(135deg, #14b8a6, #0f766e)",  # teal
    "linear-gradient(135deg, #3b82f6, #1d4ed8)",  # blue (info)
    "linear-gradient(135deg, #84cc16, #4d7c0f)",  # lime
]


def _stable_index(seed: str, modulo: int) -> int:
    """md5, а не встроенный hash() — у str-хэша в Python рандомная соль на
    процесс (PYTHONHASHSEED), один и тот же игрок красился бы в разные
    цвета при каждом перезапуске воркера/деплое."""
    digest = hashlib.md5((seed or "?").encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % modulo


@register.filter
def avatar_gradient(name: str) -> str:
    """CSS background для генеративного аватара — стабильный по имени."""
    return _GRADIENTS[_stable_index(name, len(_GRADIENTS))]


@register.filter
def avatar_initials(name: str) -> str:
    """Первые буквы первых двух слов имени, заглавные — 'Тимур Құрбанов' ->
    'ТҚ'. Одно слово — одна буква. Пусто — '?' (не должно встречаться в
    реальных данных, просто чтобы не падал шаблон)."""
    parts = (name or "").split()
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][0].upper()
    return (parts[0][0] + parts[1][0]).upper()
