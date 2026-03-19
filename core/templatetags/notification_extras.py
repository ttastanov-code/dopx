# core/templatetags/notification_extras.py
from django import template

register = template.Library()

@register.filter
def unread_count(queryset):
    """Подсчитывает непрочитанные уведомления"""
    return queryset.filter(is_read=False).count()