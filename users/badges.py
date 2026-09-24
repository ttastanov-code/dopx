# users/badges.py
"""Каталог достижений (BADGE_CATALOG) — единый источник для choices UserBadge,
отображения (редкость, секретность) и описаний. Критерии выдачи — users/services.py.
rarity/is_secret читаются из каталога через properties UserBadge.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Rarity = Literal["bronze", "silver", "gold", "platinum", "secret", "legendary"]

RARITY_ORDER: dict[Rarity, int] = {
    "bronze": 1,
    "silver": 2,
    "gold": 3,
    "platinum": 4,
    "secret": 5,
    # legendary — самые сложные; secret — скрыты до получения (не обязательно сложные).
    "legendary": 6,
}


@dataclass(frozen=True)
class BadgeDefinition:
    code: str
    name: str
    description: str
    rarity: Rarity
    is_secret: bool = False


BADGE_CATALOG: dict[str, BadgeDefinition] = {
    # --- Вовлечённость ---
    "first_evaluation": BadgeDefinition(
        code="first_evaluation",
        name="Первая оценка",
        description="Оценили свой первый матч на DOPX.",
        rarity="bronze",
    ),
    "active_fan_10": BadgeDefinition(
        code="active_fan_10",
        name="Активный фанат",
        description="Оценили 10 матчей.",
        rarity="bronze",
    ),
    "active_fan_50": BadgeDefinition(
        code="active_fan_50",
        name="Хардкор фанат",
        description="Оценили 50 матчей.",
        rarity="silver",
    ),
    "active_fan_150": BadgeDefinition(
        code="active_fan_150",
        name="Легенда трибун",
        description="Оценили 150 матчей — вы часть истории платформы.",
        rarity="gold",
    ),
    # Серия — по турам, а не по дням.
    "streak_7": BadgeDefinition(
        code="streak_7",
        name="Верный трибунам",
        description="Оценили матч 7 туров чемпионата подряд.",
        rarity="bronze",
    ),
    "streak_30": BadgeDefinition(
        code="streak_30",
        name="Сезонный болельщик",
        description="Оценили матч 30 туров чемпионата подряд.",
        rarity="silver",
    ),
    "streak_100": BadgeDefinition(
        code="streak_100",
        name="Железная дисциплина",
        description="Оценили матч 100 туров чемпионата подряд.",
        rarity="platinum",
    ),
    # --- Качество и точность ---
    "accurate_analyst": BadgeDefinition(
        code="accurate_analyst",
        name="Точный аналитик",
        description="Ваши оценки стабильно близки к консенсусу сообщества.",
        rarity="silver",
    ),
    "foresight": BadgeDefinition(
        code="foresight",
        name="Провидец",
        description="Высокий устойчивый trust score на большой выборке оценок.",
        rarity="gold",
    ),
    "bias_free": BadgeDefinition(
        code="bias_free",
        name="Без предвзятости",
        description="Оцениваете свою команду и соперника объективно.",
        rarity="silver",
    ),
    "early_bird": BadgeDefinition(
        code="early_bird",
        name="Ранняя пташка",
        description="Одними из первых оцениваете матчи после финального свистка.",
        rarity="bronze",
    ),
    "judge_of_judges": BadgeDefinition(
        code="judge_of_judges",
        name="Судья судей",
        description="Оценили судейство 25+ матчей.",
        rarity="bronze",
    ),
    "polyglot": BadgeDefinition(
        code="polyglot",
        name="Полиглот лиги",
        description="Оценили игроков 8+ разных команд КПЛ — широкий, непредвзятый взгляд.",
        rarity="gold",
    ),
    # --- Прогнозы ---
    "first_prediction": BadgeDefinition(
        code="first_prediction",
        name="Первый прогноз",
        description="Поставили свой первый прогноз на исход матча.",
        rarity="bronze",
    ),
    # Серия прогнозов — подряд угаданные исходы.
    "prediction_streak_7": BadgeDefinition(
        code="prediction_streak_7",
        name="Аналитик недели",
        description="Угадали исход 7 матчей подряд.",
        rarity="bronze",
    ),
    "prediction_streak_30": BadgeDefinition(
        code="prediction_streak_30",
        name="Штатный прогнозист",
        description="Угадали исход 30 матчей подряд.",
        rarity="silver",
    ),
    "prediction_streak_100": BadgeDefinition(
        code="prediction_streak_100",
        name="Оракул трибун",
        description="Угадали исход 100 матчей подряд.",
        rarity="platinum",
    ),
    # --- Дерби и статусные ---
    "derby_hunter": BadgeDefinition(
        code="derby_hunter",
        name="Дерби-эксперт",
        description="Оценили 5+ матчей между принципиальными соперниками.",
        rarity="gold",
    ),
    "monthly_champion": BadgeDefinition(
        code="monthly_champion",
        name="Чемпион месяца",
        description="Заняли 1-е место по числу оценок за календарный месяц.",
        rarity="platinum",
    ),
    # --- Секретные / статусные ---
    "founder": BadgeDefinition(
        code="founder",
        name="Первопроходец",
        description="Один из первых 500 пользователей DOPX.",
        rarity="secret",
        is_secret=True,
    ),

    # --- Оценки и прогнозы (новые) ---
    "coach_expert": BadgeDefinition(
        code="coach_expert",
        name="Тренерский эксперт",
        description="Оценили работу тренеров в 25+ матчах.",
        rarity="bronze",
    ),
    "both_sides": BadgeDefinition(
        code="both_sides",
        name="Обе стороны",
        description="В 15+ матчах оценили игроков ОБЕИХ команд, а не только своих.",
        rarity="silver",
    ),
    "full_season": BadgeDefinition(
        code="full_season",
        name="Полный сезон",
        description="Оценили хотя бы один матч в каждом туре целого сезона.",
        rarity="gold",
    ),
    "stable_hand": BadgeDefinition(
        code="stable_hand",
        name="Стабильная рука",
        description="50+ прогнозов с точностью 85% и выше.",
        rarity="silver",
    ),
    "derby_prophet": BadgeDefinition(
        code="derby_prophet",
        name="Дерби-пророк",
        description="Угадали исход 5+ матчей между принципиальными соперниками.",
        rarity="gold",
    ),
    "against_the_tide": BadgeDefinition(
        code="against_the_tide",
        name="Против течения",
        description="Угадали исход, когда ваш прогноз был в меньшинстве голосов сообщества.",
        rarity="gold",
    ),
    # --- Legendary ---
    "perfect_tour": BadgeDefinition(
        code="perfect_tour",
        name="Идеальный тур",
        description="Угадали исход АБСОЛЮТНО ВСЕХ матчей одного тура чемпионата.",
        rarity="legendary",
    ),
    "streak_250": BadgeDefinition(
        code="streak_250",
        name="Живая легенда трибун",
        description="Оценили матч 250 туров чемпионата подряд.",
        rarity="legendary",
    ),
    "prediction_streak_200": BadgeDefinition(
        code="prediction_streak_200",
        name="Абсолютный оракул",
        description="Угадали исход 200 матчей подряд.",
        rarity="legendary",
    ),
    "season_completionist": BadgeDefinition(
        code="season_completionist",
        name="Стоглазый",
        description="Оценили ВСЕ завершённые матчи одного полного сезона — без единого пропуска.",
        rarity="legendary",
    ),
    "max_trust": BadgeDefinition(
        code="max_trust",
        name="Максимальное доверие",
        description="Достигли максимального уровня доверия платформы на большой выборке оценок.",
        rarity="legendary",
    ),
}

BADGE_TYPE_CHOICES: list[tuple[str, str]] = [(code, d.name) for code, d in BADGE_CATALOG.items()]


def get_badge_definition(code: str) -> BadgeDefinition | None:
    return BADGE_CATALOG.get(code)