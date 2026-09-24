# core/templatetags/asset_extras.py
"""{% static_v 'css/match-detail.css' %} — как {% static %}, но с ?v=<mtime файла>,
чтобы после правки браузер/nginx не отдавали старую версию.
"""
import os

from django import template
from django.conf import settings
from django.contrib.staticfiles import finders
from django.templatetags.static import static as static_url

register = template.Library()


@register.simple_tag
def static_v(path):
    url = static_url(path)

    abs_path = finders.find(path)
    if abs_path is None and settings.STATIC_ROOT:
        candidate = os.path.join(str(settings.STATIC_ROOT), path)
        if os.path.exists(candidate):
            abs_path = candidate

    if not abs_path or not os.path.exists(abs_path):
        return url

    try:
        version = int(os.path.getmtime(abs_path))
    except OSError:
        return url

    separator = "&" if "?" in url else "?"
    return f"{url}{separator}v={version}"


# Тег Django-сообщения -> имя иконки Tabler.
_MESSAGE_ICON_BY_TAG = {
    "debug": "bug",
    "info": "info-circle",
    "success": "circle-check",
    "warning": "alert-triangle",
    "error": "alert-circle",
}


@register.filter
def message_icon(tags):
    """Иконка для flash-сообщения по тегу (success/error/... — не имена иконок Tabler)."""
    return _MESSAGE_ICON_BY_TAG.get(tags, "info-circle")
