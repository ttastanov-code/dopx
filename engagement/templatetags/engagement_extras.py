# engagement/templatetags/engagement_extras.py
"""Косметика в шаблонах: рамка аватара, золотой ник (абонемент) и звание по медалям."""
from django import template

register = template.Library()


@register.filter
def frame_class(user) -> str:
    """CSS-класс рамки аватара: dx-frame dx-frame--gold (или пусто)."""
    from engagement.season import cosmetics

    if not getattr(user, "pk", None):
        return ""
    from users.prestige import prestige

    # Лучшая из двух: рамка абонемента или рамка по самой редкой медали.
    order = {"bronze": 1, "silver": 2, "gold": 3, "platinum": 4, "legendary": 5}
    frame = max((cosmetics(user.pk)["frame"], prestige(user.pk)["frame"]), key=lambda f: order.get(f, 0))
    return f"dx-frame dx-frame--{frame}" if frame else ""


@register.filter
def name_class(user) -> str:
    """Золотой ник за полностью пройденный абонемент."""
    from engagement.season import cosmetics

    if not getattr(user, "pk", None):
        return ""
    return "dx-golden-name" if cosmetics(user.pk)["golden_name"] else ""


@register.inclusion_tag("components/_user_title.html")
def user_title(user, size="sm"):
    """Звание у ника: мини-медаль и название самой ценной (или выбранной) награды — с золота и выше."""
    from users.prestige import prestige

    title = prestige(user.pk)["title"] if getattr(user, "pk", None) else None
    return {"title": title, "size": size}
