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

from aggregates.services import CONFIDENT_VOTES_THRESHOLD, MIN_VOTES_FOR_DISPLAY

register = template.Library()

# Пороги для человекочитаемого лейбла разброса мнений поверх
# stability_index = 1/std_dev (aggregates/services.py::recalculate_player_aggregate).
# На шкале оценок 1..10: std_dev < 1.0 (index > 1.0) — зрители в целом
# согласны; std_dev 1.0..2.0 (index 0.5..1.0) — заметные расхождения;
# std_dev > 2.0 (index < 0.5) — мнения разделились почти пополам.
STABILITY_HIGH_THRESHOLD = 1.0
STABILITY_LOW_THRESHOLD = 0.5


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


def _confidence_tier(total_votes) -> str:
    try:
        n = int(total_votes or 0)
    except (TypeError, ValueError):
        n = 0
    if n < MIN_VOTES_FOR_DISPLAY:
        return "preliminary"
    if n < CONFIDENT_VOTES_THRESHOLD:
        return "basic"
    return "high"


_TIER_META = {
    "preliminary": {"label": "Предварительно", "badge_class": "badge-ghost"},
    "basic": {"label": "Есть данные", "badge_class": "badge-info badge-outline"},
    "high": {"label": "Высокая надёжность", "badge_class": "badge-success badge-outline"},
}


@register.filter
def stability_label(stability_index) -> str:
    """Человекочитаемый лейбл разброса мнений поверх stability_index = 1/std_dev."""
    try:
        value = float(stability_index)
    except (TypeError, ValueError):
        return ""
    if value >= STABILITY_HIGH_THRESHOLD:
        return "мнения сходятся"
    if value >= STABILITY_LOW_THRESHOLD:
        return "мнения расходятся"
    return "мнения расходятся сильно"


@register.inclusion_tag("components/_confidence_badge.html")
def confidence_badge(aggregate):
    """
    Градуированный индикатор надёжности рейтинга (продуктовый аудит "доверие
    к рейтингу", 2026-08-21): вместо бинарного has_enough_votes — три уровня
    с тултипом, объясняющим число голосов, разброс мнений (stability_index)
    и разбивку по лагерям (own_fans_avg/rival_fans_avg/neutral_avg). НЕ меняет
    существующий гейт "показывать ли число" (за это по-прежнему отвечает
    has_enough_votes/MIN_VOTES_FOR_DISPLAY в шаблонах) — только дополняет его
    объяснением, добавляется рядом с уже существующим числом/бейджем "н/д".
    """
    if aggregate is None:
        return {"show": False}

    total_votes = getattr(aggregate, "total_votes", 0) or 0
    tier = _confidence_tier(total_votes)
    meta = _TIER_META[tier]

    tooltip_parts = [f"{total_votes} голос(ов)."]
    stability_text = stability_label(getattr(aggregate, "stability_index", None))
    if stability_text:
        tooltip_parts.append(f"Разброс мнений: {stability_text}.")
    segment_text = bias_segment_text(aggregate)
    if segment_text:
        tooltip_parts.append(segment_text + ".")
    if tier == "preliminary":
        remaining = votes_needed(total_votes)
        tooltip_parts.append(
            f"Нужно ещё {remaining}, чтобы рейтинг считался статистически представительным."
        )

    return {
        "show": True,
        "tier": tier,
        "tier_label": meta["label"],
        "badge_class": meta["badge_class"],
        "tooltip_text": " ".join(tooltip_parts),
    }
