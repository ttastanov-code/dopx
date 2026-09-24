# core/templatetags/rating_extras.py
"""Теги для рейтингов: порог голосов для показа, бейдж надёжности, тултип по лагерям.
Ниже MIN_VOTES_FOR_DISPLAY рейтинг числом не показываем.
"""
from django import template

from aggregates.services import CONFIDENT_VOTES_THRESHOLD, MIN_VOTES_FOR_DISPLAY
from core.models import get_setting

register = template.Library()

# Пороги — из настроек платформы (get_setting, кэш 60 с), константы — fallback.


def _min_votes_for_display() -> int:
    return get_setting("min_votes_for_display", MIN_VOTES_FOR_DISPLAY)


def _confident_votes_threshold() -> int:
    return get_setting("confident_votes_threshold", CONFIDENT_VOTES_THRESHOLD)

# Пороги разброса мнений по stability_index = 1/std_dev.
STABILITY_HIGH_THRESHOLD = 1.0
STABILITY_LOW_THRESHOLD = 0.5


@register.filter
def has_enough_votes(total_votes) -> bool:
    """Достаточно ли голосов для показа рейтинга."""
    try:
        return int(total_votes or 0) >= _min_votes_for_display()
    except (TypeError, ValueError):
        return False


@register.simple_tag
def votes_needed(total_votes) -> int:
    """Сколько голосов не хватает до порога."""
    threshold = _min_votes_for_display()
    try:
        remaining = threshold - int(total_votes or 0)
    except (TypeError, ValueError):
        remaining = threshold
    return max(0, remaining)


@register.simple_tag
def bias_segment_text(aggregate) -> str:
    """Текст тултипа: средние у своих болельщиков, соперника и нейтральных.
    Пустая строка, если сегментов меньше двух.
    """
    if aggregate is None:
        return ""
    parts = []
    if aggregate.own_fans_avg is not None:
        parts.append(f"свои болельщики — {aggregate.own_fans_avg:.1f}")
    if aggregate.rival_fans_avg is not None:
        parts.append(f"болельщики соперника — {aggregate.rival_fans_avg:.1f}")
    if aggregate.neutral_avg is not None:
        parts.append(f"нейтральные зрители — {aggregate.neutral_avg:.1f}")
    if len(parts) < 2:
        # Меньше 2 сегментов — сравнивать не с чем.
        return ""
    return ", ".join(parts)


def _confidence_tier(total_votes) -> str:
    try:
        n = int(total_votes or 0)
    except (TypeError, ValueError):
        n = 0
    if n < _min_votes_for_display():
        return "preliminary"
    if n < _confident_votes_threshold():
        return "basic"
    return "high"


# Короткие подписи бейджа, в каждой слово «оценок».
# Держать в синхроне с легендой в templates/core/anti_fraud.html.
_TIER_META = {
    "preliminary": {"label": "Мало оценок", "badge_class": "badge-ghost"},
    "basic": {"label": "Оценок хватает", "badge_class": "badge-info badge-outline"},
    "high": {"label": "Оценок много", "badge_class": "badge-success badge-outline"},
}


@register.filter
def stability_label(stability_index) -> str:
    """Прилагательное для разброса мнений (слово «мнения» добавляет confidence_badge)."""
    try:
        value = float(stability_index)
    except (TypeError, ValueError):
        return ""
    if value >= STABILITY_HIGH_THRESHOLD:
        return "сходятся"
    if value >= STABILITY_LOW_THRESHOLD:
        return "расходятся"
    return "расходятся сильно"


@register.inclusion_tag("components/_confidence_badge.html")
def confidence_badge(aggregate):
    """Бейдж надёжности рейтинга (3 уровня) с тултипом: голоса, разброс, лагеря.
    Не заменяет has_enough_votes.
    """
    if aggregate is None:
        return {"show": False}

    total_votes = getattr(aggregate, "total_votes", 0) or 0
    tier = _confidence_tier(total_votes)
    meta = _TIER_META[tier]
    # Число оценок в самом бейдже — только для preliminary.
    tier_label = f"{meta['label']} · {total_votes}" if tier == "preliminary" else meta["label"]

    # Разброс и лагеря — одним предложением.
    tooltip_parts = [f"{total_votes} голос(ов)."]
    stability_text = stability_label(getattr(aggregate, "stability_index", None))
    segment_text = bias_segment_text(aggregate)
    if stability_text and segment_text:
        tooltip_parts.append(f"Мнения {stability_text}: {segment_text}.")
    elif stability_text:
        tooltip_parts.append(f"Мнения {stability_text}.")
    elif segment_text:
        tooltip_parts.append(f"{segment_text[0].upper()}{segment_text[1:]}.")
    if tier == "preliminary":
        remaining = votes_needed(total_votes)
        tooltip_parts.append(
            f"Нужно ещё {remaining}, чтобы рейтинг считался статистически представительным."
        )

    return {
        "show": True,
        "tier": tier,
        "tier_label": tier_label,
        "badge_class": meta["badge_class"],
        "tooltip_text": " ".join(tooltip_parts),
    }
