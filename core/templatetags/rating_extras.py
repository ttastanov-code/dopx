# core/templatetags/rating_extras.py
"""
Продуктовый аудит, раздел 1 ("Целостность данных важнее новых фич"):
минимальный порог голосов для показа рейтинга игрока/тренера.

ПРОБЛЕМА: `PlayerMatchAggregate.performance_score` считается и
показывается даже при `total_votes == 1` — один зритель (иногда сам
игрок или его друг) может вручную выставить любую оценку, и число
"10.0" на странице выглядит так же авторитетно, как настоящий
консенсус сотен зрителей. Это подрывает доверие к платформе больше,
чем полное отсутствие данных: пользователь либо принимает
непредставительное число за правду, либо (обнаружив подвох) перестаёт
доверять ЛЮБЫМ цифрам на сайте, включая честно посчитанные.

РЕШЕНИЕ: единый порог `MIN_VOTES_FOR_DISPLAY`, ниже которого рейтинг
не показывается как число вообще — вместо него бейдж "Недостаточно
данных". Один источник истины (эта константа), а не magic number,
разбросанный по шаблонам.
"""
from django import template

from aggregates.services import MIN_VOTES_FOR_DISPLAY

register = template.Library()


@register.filter
def has_enough_votes(total_votes) -> bool:
    """True, если голосов достаточно, чтобы показывать рейтинг как число."""
    try:
        return int(total_votes or 0) >= MIN_VOTES_FOR_DISPLAY
    except (TypeError, ValueError):
        return False


@register.simple_tag
def votes_needed(total_votes) -> int:
    """Сколько ещё голосов не хватает до порога показа (для UI-подсказки)."""
    try:
        remaining = MIN_VOTES_FOR_DISPLAY - int(total_votes or 0)
    except (TypeError, ValueError):
        remaining = MIN_VOTES_FOR_DISPLAY
    return max(0, remaining)


@register.simple_tag
def bias_segment_text(aggregate) -> str:
    """
    Текст для _tooltip_icon.html — сколько поставили "свои", "чужие" и
    нейтральные зрители (aggregates/services.py::_segment_by_fan_side).
    Пустая строка, если сегментов ещё нет (старые записи до миграции
    0002, либо все голоса из одного сегмента) — вызывающий шаблон должен
    не рисовать иконку в этом случае.
    """
    if aggregate is None:
        return ""
    parts = []
    if aggregate.own_fans_avg is not None:
        parts.append(f"фанаты игрока: {aggregate.own_fans_avg:.1f}")
    if aggregate.rival_fans_avg is not None:
        parts.append(f"фанаты соперника: {aggregate.rival_fans_avg:.1f}")
    if aggregate.neutral_avg is not None:
        parts.append(f"нейтральные: {aggregate.neutral_avg:.1f}")
    if len(parts) < 2:
        # Меньше 2 сегментов — сравнивать не с чем, подсказка бесполезна.
        return ""
    return "Разбивка по лагерям — " + ", ".join(parts)
