# core/templatetags/match_extras.py
from django import template

register = template.Library()

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