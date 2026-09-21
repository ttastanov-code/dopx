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


# Django messages.tags ('debug'/'info'/'success'/'warning'/'error') -> реальное
# имя иконки в наборе Tabler Icons (см. докстринг message_icon() ниже).
_MESSAGE_ICON_BY_TAG = {
    "debug": "bug",
    "info": "info-circle",
    "success": "circle-check",
    "warning": "alert-triangle",
    "error": "alert-circle",
}


@register.filter
def message_icon(tags):
    """БАГ, КОТОРЫЙ ТУТ БЫЛ (найдено 2026-09-21, UI/UX-аудит base.html и
    base_auth.html): иконка возле каждого flash-сообщения рисовалась как
    `class="ti ti-{{ message.tags|default:'info' }}"` — то есть тег
    Django-сообщения (debug/info/success/warning/error) подставлялся
    НАПРЯМУЮ как имя иконки Tabler. Проверено прямой выгрузкой реального
    tabler-icons.min.css из того же CDN-пакета (@tabler/icons-webfont@
    3.46.0), что подключён в <head>: классов `.ti-success`, `.ti-error`,
    `.ti-warning` в нём НЕТ вообще — у Tabler для этих смыслов другие,
    более длинные имена (circle-check, alert-circle, alert-triangle). А
    `.ti-debug` не существует ни в каком виде. `.ti-info` — реальный
    класс, но это другая, более скромная пиктограмма, чем `info-circle`,
    которым по всему остальному сайту обозначают именно "информация"
    (components/_tooltip_icon.html и т.д.) — то есть даже "рабочий" по
    случайности вариант выглядел не как остальные info-иконки сайта.

    Итог до фикса: иконка перед КАЖДЫМ flash-сообщением на сайте (успех
    после сохранения формы, ошибка входа, предупреждение и т.д.) — на
    обеих раскладках, base.html и base_auth.html — либо не отображала
    вообще никакого символа (invalid class = невидимый пустой квадрат
    шрифтовой иконки), либо отображала не тот символ, что предполагался.

    Фикс: явный маппинг тега сообщения на реальное имя иконки Tabler."""
    return _MESSAGE_ICON_BY_TAG.get(tags, "info-circle")
