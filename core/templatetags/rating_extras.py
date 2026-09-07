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

    НАЙДЕНО (2026-09-01, жалоба пользователя: "фанаты игрока" — неверно,
    и вообще подсказка нихера не понятная): "own_fans_avg" — это фанаты
    КОМАНДЫ сущности (`entity_team_id` в aggregates/services.py::
    segment_evaluations_by_side), не персонально игрока. Раньше это было
    видно только при рейтинге игрока, но `own_fans_avg`/`rival_fans_avg`
    есть и на TeamMatchAggregate, и на CoachMatchAggregate (см.
    aggregates/models.py) — там подпись "фанаты игрока" вообще не при чём
    (тренер, целая команда). Заменено на нейтральную формулировку, верную
    для игрока/тренера/команды одинаково: "свои болельщики" = болельщики
    команды этой сущности, а не лично игрока/тренера.
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
        # Меньше 2 сегментов — сравнивать не с чем, подсказка бесполезна.
        return ""
    return ", ".join(parts)


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
    """
    Человекочитаемый лейбл разброса мнений поверх stability_index = 1/std_dev.

    НАЙДЕНО (2026-09-01): раньше возвращала "мнения сходятся"/"мнения
    расходятся" целиком, а confidence_badge() ниже собирал строку
    "Разброс мнений: {label}" — получалось задвоенное "мнения" ("Разброс
    мнений: мнения расходятся"), одна из причин жалобы "тексты нихера не
    понятные". Возвращает только прилагательное, слово "мнения" — один
    раз, в самом confidence_badge.
    """
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
    # Число оценок — прямо в видимом бейдже, не только в тултипе, только
    # для "preliminary" (продуктовый бэклог "показывать число оценок и
    # пометку 'предварительный рейтинг' при маленькой выборке",
    # docs/BACKLOG.md): именно этот случай — когда пользователю важнее
    # всего сразу увидеть, что рейтинг основан на единичных голосах, не
    # заходя в тултип. Для basic/high оставляем статичный лейбл — там
    # число менее критично для доверия и не хочется загромождать таблицы.
    tier_label = f"{meta['label']} · {total_votes}" if tier == "preliminary" else meta["label"]

    # НАЙДЕНО (2026-09-01, жалоба пользователя: "тексты нихера не понятные"):
    # раньше разброс мнений и разбивка по лагерям шли двумя отдельными,
    # неловко построенными предложениями ("Разброс мнений: мнения
    # расходятся. Разбивка по лагерям — фанаты игрока: 8.0, ..." — задвоенное
    # "мнения", неверная подпись "фанаты игрока" для команды/тренера).
    # Собираем ОДНИМ читаемым предложением, когда есть оба куска: "Мнения
    # расходятся: свои болельщики — 8.0, болельщики соперника — 7.0,
    # нейтральные зрители — 7.0." — сразу видно И вывод, И на чём он основан.
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
