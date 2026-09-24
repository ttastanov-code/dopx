# core/templatetags/name_extras.py
"""Фамилия для компактных мест (виджет сборной)."""
from django import template

register = template.Library()


@register.filter
def surname(name: str) -> str:
    """Последнее слово имени ('Шохан Абзалов' -> 'Абзалов')."""
    parts = (name or "").split()
    return parts[-1] if parts else name
