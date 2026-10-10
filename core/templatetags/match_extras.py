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
    """Заголовок игрового дня: «Сегодня»/«Завтра»/«Вчера» или день недели (дата — matchday_date)."""
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
    return _WEEKDAYS_RU[target.weekday()].capitalize()


_MONTHS_GENITIVE_RU = (
    'января', 'февраля', 'марта', 'апреля', 'мая', 'июня',
    'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря',
)


@register.filter
def matchday_date(value):
    """«13 сентября» (+ год, если не текущий) — подпись под matchday_label."""
    if not value:
        return ''
    target = timezone.localtime(value).date() if timezone.is_aware(value) else value.date() if hasattr(value, 'date') else value
    text = f"{target.day} {_MONTHS_GENITIVE_RU[target.month - 1]}"
    if target.year != timezone.localdate().year:
        text += f" {target.year}"
    return text

@register.simple_tag
def render_score(home_score, away_score, show_zero=True):
    """Счёт: "2 : 0"; до матча "0 : 0"; show_zero — 0 вместо "-"."""
    home = home_score if home_score is not None else 0
    away = away_score if away_score is not None else 0
    return f"{home} : {away}"


@register.simple_tag
def render_score_short(home_score, away_score):
    """Короткий счёт для компактных карточек."""
    home = home_score if home_score is not None else 0
    away = away_score if away_score is not None else 0
    return f"{home}:{away}"


@register.filter
def score_value(score):
    """Отдельное значение счёта."""
    return score if score is not None else 0


@register.filter
def kickoff(match, date_fmt: str = "%d.%m"):
    """{{ match|kickoff }} — «10.10 в 16:00» или «10.10, время уточняется» (заглушку 05:00 не показываем)."""
    return match.kickoff_text(date_fmt) if match else ""


@register.filter
def team_result(match, team):
    """Исход матча для команды: «В», «Н», «П» или '' (счёта нет)."""
    if match is None or team is None or match.home_score is None or match.away_score is None:
        return ""
    own, other = ((match.home_score, match.away_score) if match.home_team_id == team.id
                  else (match.away_score, match.home_score))
    return "В" if own > other else "П" if own < other else "Н"
