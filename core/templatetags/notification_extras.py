# core/templatetags/notification_extras.py
from django import template

register = template.Library()

@register.filter
def unread_count(queryset):
    """Подсчитывает непрочитанные уведомления"""
    return queryset.filter(is_read=False).count()

@register.filter
def notif_style(notification_type):
    """{icon, color, section} для типа уведомления (notifications/styles.py)."""
    from notifications.styles import style_for

    return style_for(notification_type or "")


_LEADING_EMOJI = __import__("re").compile("^[\U0001F000-\U0001FAFF←-⯿️‍⃣]+\\s*")


@register.filter
def strip_emoji(text):
    """Без эмодзи в начале: на сайте тип уже показан иконкой, эмодзи нужны только в пушах."""
    return _LEADING_EMOJI.sub("", text or "")


@register.filter
def ago_short(dt):
    """Короткое время для ленты: «сейчас», «5 мин», «3 ч», «вчера», «4 дн», «14 окт»."""
    from django.utils import timezone

    if not dt:
        return ""
    now = timezone.now()
    seconds = (now - dt).total_seconds()
    if seconds < 60:
        return "сейчас"
    if seconds < 3600:
        return f"{int(seconds // 60)} мин"
    local, today = timezone.localtime(dt).date(), timezone.localdate()
    if local == today:
        return f"{int(seconds // 3600)} ч"
    days = (today - local).days
    if days == 1:
        return "вчера"
    if days < 7:
        return f"{days} дн"
    months = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]
    label = f"{local.day} {months[local.month - 1]}"
    return label if local.year == today.year else f"{label} {local.year}"
