# core/templatetags/position_extras.py
"""Шаблонный фильтр для человекочитаемых позиций игроков — см. players/positions.py."""
from django import template

from players.positions import position_label

register = template.Library()


@register.filter(name="position_label")
def position_label_filter(code):
    """{{ player.position|position_label }} — вернёт "Вратарь" и т.п.,
    либо пустую строку для нераспознанного кода (шаблон дальше решает,
    через |default:"—", что показать вместо пустоты)."""
    return position_label(code) or ""
