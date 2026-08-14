# core/templatetags/match_extras.py
from django import template
from django.utils import timezone

register = template.Library()

_WEEKDAYS_RU = [
    'понедельник', 'вторник', 'среда', 'четверг',
    'пятница', 'суббота', 'воскресенье',
]


@register.filter
def matchday_label(value):
    """
    Человекочитаемый заголовок игрового дня для группировки списка матчей
    (как в sofascore/fotmob: "Сегодня", "Завтра", "Вчера", иначе дата с
    днём недели). Используется в matches/list.html через {% regroup %},
    группирующий соседние по дате матчи под общим заголовком — вместо
    плоского списка из 20 карточек, где дату матча приходится читать
    в каждой карточке отдельно.
    """
    if not value:
        return ''
    target = timezone.localtime(value).date() if timezone.is_aware(value) else value.date() if hasattr(value, 'date') else value
    today = timezone.localdate()
    delta = (target - today).days
    if delta == 0:
        return 'Сегодня'
    if delta == 1:
        return 'Завтра'
    if delta == -1:
        return 'Вчера'
    weekday = _WEEKDAYS_RU[target.weekday()]
    return f"{target.strftime('%d.%m.%Y')}, {weekday}"

@register.simple_tag
def render_score(home_score, away_score, show_zero=True):
    """
    Корректное отображение счёта:
    - Если оба счёта известны: "2 : 0"
    - Если матч не начался: "0 : 0"
    - show_zero=True показывает 0 вместо "-"
    """
    home = home_score if home_score is not None else 0
    away = away_score if away_score is not None else 0
    return f"{home} : {away}"


@register.simple_tag
def render_score_short(home_score, away_score):
    """Короткое отображение счёта для компактных карточек"""
    home = home_score if home_score is not None else 0
    away = away_score if away_score is not None else 0
    return f"{home}:{away}"


@register.filter
def score_value(score):
    """Фильтр для отображения отдельного значения счёта"""
    return score if score is not None else 0