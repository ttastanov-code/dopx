# engagement/templatetags/engagement_extras.py
"""Косметика абонемента в шаблонах: рамка аватара и золотой ник."""
from django import template

register = template.Library()


@register.filter
def frame_class(user) -> str:
    """CSS-класс рамки аватара: dx-frame dx-frame--gold (или пусто)."""
    from engagement.season import cosmetics

    if not getattr(user, "pk", None):
        return ""
    frame = cosmetics(user.pk)["frame"]
    return f"dx-frame dx-frame--{frame}" if frame else ""


@register.filter
def name_class(user) -> str:
    """Золотой ник за полностью пройденный абонемент."""
    from engagement.season import cosmetics

    if not getattr(user, "pk", None):
        return ""
    return "dx-golden-name" if cosmetics(user.pk)["golden_name"] else ""
