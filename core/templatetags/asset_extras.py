# core/templatetags/asset_extras.py
"""
{% static_v 'css/match-detail.css' %} — как {% static %}, но добавляет
?v=<mtime файла> к URL.

ПОЧЕМУ ЭТО ПОЯВИЛОСЬ (2026-08-31): в base.html свои CSS/JS всегда
подключались обычным {% static %} без версии в URL. Пока файл не менялся
— это ок, но при каждой правке CSS (а за эту сессию их было очень много —
match-detail.css переделывался несколько раз) браузер и любой
промежуточный кэш (nginx в проде, см. nginx.conf) продолжали отдавать
СТАРУЮ закэшированную версию файла по тому же самому URL. Конкретный
кейс, из-за которого это добавили: после отката автоцветов клубов и
добавления тиснения гербов (.md-hero__emboss) в match-detail.css —
обычная навигация по сайту показывала герб огромным, ярким и не на
своём месте, потому что грузился css-файл ДО правки, где класса
.md-hero__emboss ещё не существовало. Обычный hard refresh (Cmd/Ctrl+
Shift+R) чинил это ровно один раз, а при следующем обычном переходе всё
возвращалось — классический симптом закэшированного статик-файла без
cache-busting.

Как работает: berём реальный путь файла на диске (через
staticfiles.finders — работает в DEBUG и локально; в проде после
collectstatic файл может быть уже только в STATIC_ROOT, тогда fallback
ищет его там), считаем mtime, добавляем как ?v=<unix timestamp> к URL.
Файл поменялся — mtime поменялся — URL поменялся — браузер/nginx
гарантированно качают свежую версию, никому не нужно руками бампать
версию при каждой правке.
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
