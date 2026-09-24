# core/templatetags/avatar_extras.py
"""Генеративный аватар: детерминированный градиент + инициалы.
Фото, загруженное вручную, по-прежнему показывается.
"""
from __future__ import annotations

import hashlib

from django import template

register = template.Library()

# Палитра под цвета daisyUI-темы.
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
    """md5, а не hash() — hash() меняется между процессами."""
    digest = hashlib.md5((seed or "?").encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % modulo


@register.filter
def avatar_gradient(name: str) -> str:
    """CSS background аватара — стабилен по имени."""
    return _GRADIENTS[_stable_index(name, len(_GRADIENTS))]


@register.filter
def avatar_initials(name: str) -> str:
    """Инициалы: первые буквы двух слов ('Тимур Құрбанов' -> 'ТҚ'), пусто — '?'."""
    parts = (name or "").split()
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][0].upper()
    return (parts[0][0] + parts[1][0]).upper()
