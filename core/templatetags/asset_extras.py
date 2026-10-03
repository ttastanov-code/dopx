# core/templatetags/asset_extras.py
"""{% static_v 'css/match-detail.css' %} — как {% static %}, но с ?v=<mtime файла>,
чтобы после правки браузер/nginx не отдавали старую версию.
"""
import os

from django import template
from django.conf import settings
from django.contrib.staticfiles import finders
from django.templatetags.static import static as static_url
from django.utils.html import format_html
from django.utils.safestring import mark_safe

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


_TAILWIND_CDN = (
    '<script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4.3.3" integrity="sha384-aJ9rL4k6lF+91guGvUFVSkpIcge7Zd9EiI4TQDLoK9kFaFJgKHgjEXVvG/qA5COj" crossorigin="anonymous"></script>\n'
    '    <link href="https://cdn.jsdelivr.net/npm/daisyui@5.7.17" rel="stylesheet" type="text/css" integrity="sha384-39M1LQqzp+SSirSevKD4gsadFB7kgKzMiPY7o6hVhOXuRthgtd1oohKwlosU9KHi" crossorigin="anonymous" />\n'
    '    <link href="https://cdn.jsdelivr.net/npm/daisyui@5.7.17/themes.css" rel="stylesheet" type="text/css" integrity="sha384-HhSoSsQclUlsqy8Gyz+E2oVSebHT4dv3MJ+NdUjV9mh1twUK4UPKQgmj0dHDKe0Y" crossorigin="anonymous" />'
)


@register.simple_tag
def tailwind_assets():
    """Стили Tailwind + daisyUI: готовый app.css (собирается в Docker) или, при TAILWIND_CDN, сборка в браузере —
    на ноутбуке, чтобы новые классы в шаблонах работали без пересборки."""
    if getattr(settings, "TAILWIND_CDN", False):
        return mark_safe(_TAILWIND_CDN)
    return format_html('<link rel="stylesheet" href="{}">', static_v("css/app.css"))


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
