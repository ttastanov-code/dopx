# core/templatetags/ui_extras.py
"""Мелкие фильтры интерфейса."""
from django import template

register = template.Library()


@register.filter
def ru_plural(number, forms: str) -> str:
    """{{ n|ru_plural:"тур,тура,туров" }} — форма слова по числу (1 / 2-4 / 5+)."""
    try:
        n = abs(int(number))
    except (TypeError, ValueError):
        return ""
    one, few, many = (forms.split(",") + ["", "", ""])[:3]
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


@register.filter
def ago(value) -> str:
    """{{ dt|ago }} — «только что» меньше минуты назад, иначе «N минут назад»."""
    from django.utils import timezone
    from django.utils.timesince import timesince

    if not value:
        return ""
    if (timezone.now() - value).total_seconds() < 60:
        return "только что"
    return f"{timesince(value)} назад"


@register.filter
def autohide_ms(messages) -> int:
    """Через сколько мс скрыть flash-сообщения: ошибки читаются дольше."""
    return 9000 if any(m.level >= 40 for m in messages) else 5000


@register.filter
def nomination_groups(nominations) -> list:
    """Номинации по категориям (судьи, команды, …) — см. core.nominations.group_nominations."""
    from core.nominations import group_nominations
    return group_nominations(nominations)


@register.filter
def nomination_highlights(nominations) -> list:
    """Лучший в каждой категории — вкладка «Главное»."""
    from core.nominations import nomination_highlights as highlights
    return highlights(nominations)
