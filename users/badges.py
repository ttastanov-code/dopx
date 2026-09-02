# users/badges.py
"""
Единый каталог достижений платформы — НОВЫЙ модуль.

ПОЧЕМУ ОТДЕЛЬНЫЙ ФАЙЛ, А НЕ `BADGE_TYPES` ПРЯМО В `models.py`: раньше
"каталог" достижений — это была одна строка `BADGE_TYPES = [(code, label),
...]` в `UserBadge`, а вся смысловая начинка (описание для пользователя,
редкость, секретность) нигде не хранилась — фронтенду было физически не из
чего взять "золотое" или "бронзовое" оформление бейджа, кроме как хардкодить
маппинг где-то в шаблоне и вручную дублировать список. Теперь один
`BadgeDefinition` на достижение — единственный источник истины: и для
`choices` модели, и для рендера в профиле, и для критерия начисления в
`users/services.py`.

`rarity`/`is_secret` НЕ хранятся как отдельные колонки в `UserBadge` —
это осознанное решение, чтобы не тянуть миграцию под две колонки:
`UserBadge.rarity`/`UserBadge.is_secret` — это properties, читающие
метаданные из `BADGE_CATALOG` по уже существующему полю `badge_type`.
Единственное, что действительно меняется в схеме БД — расширение списка
`choices` у существующего `badge_type` (это не меняет тип/длину колонки,
Django всё равно попросит миграцию state-файла — выполните
`python manage.py makemigrations users` перед деплоем).

НОВЫЕ ДОСТИЖЕНИЯ (6 новых поверх существовавших 8) отобраны по единственному
критерию: их можно посчитать по данным, которые в проекте УЖЕ есть, без
новых внешних пайплайнов данных.

ВТОРОЙ ЗАХОД (после продуктового аудита, раздел 7 — "отложено"): добавлены
`derby_hunter` и `monthly_champion` — оба требовали продуктовых решений,
которые нельзя было принять за пользователя молча:
- `derby_hunter` — какие команды считаются соперниками, решает админ
  (`Team.rivals` в `teams/models.py`, проставляется один раз в админке).
- `monthly_champion` — разовый, "первое место", без хранения истории по
  месяцам (см. `users/tasks.py::award_monthly_champion_badge`).
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
    # НОВОЕ (2026-09-01, продуктовый запрос "супер ультра" достижения):
    # "legendary" — про СЛОЖНОСТЬ получения (топ-уровень над platinum), а
    # не про секретность, поэтому стоит ВЫШЕ "secret" по порядку — "secret"
    # исторически означает "скрыт до получения" (founder — секретный, но
    # получить его легко, просто рано зарегистрироваться), а не "трудный".
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
    # ПЕРЕСМОТРЕНО 2026-08-31: серия считается по ТУРАМ чемпионата, а не
    # по календарным дням (матчи бывают 1–2 дня в неделю — дневная серия
    # почти ни у кого не росла бы, см. докстринг User.evaluation_streak,
    # users/models.py). Названия/описания приведены в соответствие.
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
    # --- Прогнозы (predictions app, retention loop "Серии", 2026-08-21) ---
    "first_prediction": BadgeDefinition(
        code="first_prediction",
        name="Первый прогноз",
        description="Поставили свой первый прогноз на исход матча.",
        rarity="bronze",
    ),
    # ПЕРЕСМОТРЕНО 2026-08-31: серия прогнозов теперь означает подряд
    # УГАДАННЫЕ исходы, а не подряд дни активности (см. докстринг
    # User.prediction_streak, users/models.py) — иначе можно было бы
    # получить "Оракула трибун" просто ставя прогнозы каждый день и всегда
    # ошибаясь. Названия/описания приведены в соответствие.
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

    # --- НОВОЕ (2026-09-01, продуктовый запрос "достижения по оценкам и
    # прогнозам + супер-ультра уровень") — 6 обычных + 5 legendary. Все
    # условия считаются по данным, которые в проекте УЖЕ есть, тот же
    # принцип, что у "второго захода" выше (см. докстринг модуля) — без
    # новых внешних пайплайнов данных. Критерии — users/services.py.
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
    # --- Legendary ("супер ультра") ---
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