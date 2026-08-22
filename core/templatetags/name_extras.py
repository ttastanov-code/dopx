# core/templatetags/name_extras.py
"""
Фильтр для компактных мест отображения имени (embed-виджет сборной DOPX,
templates/widgets/best_xi.html) — карточка на мини-поле шириной 60-90px не
вмещает "Имя Фамилия" целиком (players/models.py::Player.full_name), из-за
чего текст либо обрезался посередине фамилии, либо вылезал за пределы
карточки. Футбольные составы традиционно подписывают только фамилию
(как на футболке) — то же решение и здесь, плюс это ещё и короче,
поэтому лучше умещается без потери читаемости.
"""
from django import template

register = template.Library()


@register.filter
def surname(name: str) -> str:
    """Последнее слово полного имени ('Шохан Абзалов' -> 'Абзалов').
    Одно слово или пусто — возвращает как есть."""
    parts = (name or "").split()
    return parts[-1] if parts else name
