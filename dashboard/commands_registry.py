# dashboard/commands_registry.py
"""
Allowlist management-команд, которые staff может запускать из
/staff/dashboard/scripts/ (раздел "Скрипты и команды", 2026-09-22, прямая
просьба пользователя: "seed_full_history надо вывести в дашборд... команду
очистки удаления и тд, все наши тестовые скрипты и команды в отдельный
раздел").

Тот же принцип allowlist'а, что уже используется для celery-задач
(dashboard/parser_tools.py::TRIGGERABLE_TASKS) — НИКАКОГО произвольного
`manage.py <что угодно>` из UI, только команды из COMMAND_REGISTRY ниже,
с аргументами СТРОГО из их описанной схемы (dashboard/command_runner.py
валидирует и то, и другое перед call_command()).

Каждый арг описан достаточно, чтобы:
  1) сгенерировать поле формы (kind → тип input'а в scripts.html);
  2) провалидировать/привести пришедшее из POST значение к нужному типу;
  3) собрать позиционные/именованные аргументы для call_command() с
     ПРАВИЛЬНЫМ dest (argparse иногда переопределяет его явно — например
     seed_match_votes::--match-id → dest="match_ids" — простое
     "замени дефис на подчёркивание" тут дало бы неверный kwarg).

`danger`:
  - "readonly"    — ничего не меняет (diagnose_*), можно гонять без опаски.
  - "safe"        — меняет данные, но безопасно/идемпотентно/легко обратимо
                     (пересчёты, сидирование новых тестовых сущностей).
  - "destructive" — реально удаляет данные. У всех таких команд, КРОМЕ
                     cleanup_load_test, есть свой --apply (без флага —
                     dry-run отчёт, ничего не трогает) — форма всегда
                     сначала предлагает dry-run, апply — отдельный чекбокс.
                     cleanup_load_test у cамой команды такого флага нет
                     (см. её докстринг) — UI требует ручного подтверждения
                     текстом вместо чекбокса.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from matches.models import Match

_MATCH_STATUS_CHOICES = [choice[0] for choice in Match.STATUS_CHOICES]


@dataclass(frozen=True)
class ArgSpec:
    flag: str  # "--season-id" или имя позиционного ("team")
    dest: str  # ключ, под которым уйдёт в call_command()
    kind: str  # "str" | "int" | "float" | "flag" | "choice" | "list_str"
    positional: bool = False
    required: bool = False
    default: Any = None
    help: str = ""
    choices: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CommandSpec:
    name: str  # имя команды для call_command() — совпадает с именем файла
    label: str
    category: str  # "seed" | "cleanup" | "recalc" | "diagnose"
    danger: str  # "readonly" | "safe" | "destructive"
    description: str
    args: list[ArgSpec] = field(default_factory=list)
    has_apply_flag: bool = False  # есть свой --apply (dry-run по умолчанию)


CATEGORY_LABELS = {
    "seed": "Сидирование и симуляция тестовых данных",
    "cleanup": "Очистка и удаление",
    "recalc": "Пересчёт и обслуживание",
    "ai_names": "Проверка ФИО (ИИ)",
    "diagnose": "Диагностика (только чтение)",
}

COMMAND_REGISTRY: dict[str, CommandSpec] = {
    # ============================================================
    # СИДИРОВАНИЕ / СИМУЛЯЦИЯ
    # ============================================================
    "seed_full_history": CommandSpec(
        name="seed_full_history", label="Засеять полную историю сезона",
        category="seed", danger="safe", has_apply_flag=False,
        description=(
            "Главная команда для наполнения сезона тестовыми данными: оценки + "
            "прогнозы + XP + бейджи + сборные туров/сезона + турнирная таблица, "
            "по пулу переиспользуемых ботов (test_user_bot_NNNN). Идемпотентна — "
            "повторный запуск на тех же турах не плодит дубли."
        ),
        args=[
            ArgSpec("--season-id", "season_id", "str", help="UUID сезона. Пусто — активный сезон по умолчанию."),
            ArgSpec("--tour-from", "tour_from", "int", help="Не трогать туры МЕНЬШЕ этого номера."),
            ArgSpec("--tour-to", "tour_to", "int", help="Не трогать туры БОЛЬШЕ этого номера."),
            ArgSpec("--pool-size", "pool_size", "int", default=150, help="Размер пула ботов (общий с «Засеять голоса за матчи»)."),
            ArgSpec("--seed", "seed", "int", help="Seed для random — повторяемый результат при том же значении."),
            ArgSpec("--no-recalc", "no_recalc", "flag", help="Не пересчитывать агрегаты/сборные/таблицу — только сырые оценки и прогнозы."),
            ArgSpec("--no-badges", "no_badges", "flag", help="Не проверять достижения (быстрее, но бейджи не появятся)."),
        ],
    ),
    "seed_match_votes": CommandSpec(
        name="seed_match_votes", label="Засеять голоса за матчи",
        category="seed", danger="safe",
        description="Точечное голосование пула ботов за конкретные матчи (по id или по статусу+лимиту) — без полной истории сезона.",
        args=[
            ArgSpec("--match-id", "match_ids", "list_str", help="UUID матчей, по одному на строку. Пусто — используйте --status ниже."),
            ArgSpec("--status", "status", "str", help="Вместо списка id — взять матчи с этим статусом (обычно 'finished')."),
            ArgSpec("--limit", "limit", "int", default=10, help="Сколько матчей брать при использовании --status."),
            ArgSpec("--voters", "voters", "int", default=20, help="Сколько ботов проголосует за КАЖДЫЙ матч."),
            ArgSpec("--pool-size", "pool_size", "int", default=300, help="Размер пула ботов."),
            ArgSpec("--player-coverage", "player_coverage", "float", default=0.7, help="Доля голосующих, оценивающих каждого игрока (0.0-1.0)."),
            ArgSpec("--single-inflated-player", "single_inflated_player", "str", help="UUID игрока — добавить один экстремальный голос (проверка антифрода)."),
            ArgSpec("--seed", "seed", "int", help="Seed для random."),
            ArgSpec("--no-recalc", "no_recalc", "flag", help="Не пересчитывать агрегаты синхронно после сидирования."),
        ],
    ),
    "simulate_evaluations": CommandSpec(
        name="simulate_evaluations", label="Симулировать оценки пользователей",
        category="seed", danger="safe",
        description="Симулирует оценки от множества пользователей по нескольким матчам сразу.",
        args=[
            ArgSpec("--users", "users", "int", default=50, help="Количество пользователей."),
            ArgSpec("--matches", "matches", "int", default=5, help="Количество матчей."),
            ArgSpec("--match-ids", "match_ids", "str", help="Конкретные ID матчей через запятую (вместо --matches)."),
            ArgSpec("--recalculate", "recalculate", "flag", help="Пересчитать агрегаты после оценок."),
        ],
    ),
    "create_test_evaluations": CommandSpec(
        name="create_test_evaluations", label="Создать тестовые оценки для матча",
        category="seed", danger="safe",
        description="Точечно создаёт тестовые оценки для ОДНОГО конкретного матча.",
        args=[
            ArgSpec("--match-id", "match_id", "str", required=True, help="UUID матча."),
            ArgSpec("--users", "users", "int", default=5, help="Количество тестовых пользователей."),
        ],
    ),
    "create_test_users": CommandSpec(
        name="create_test_users", label="Создать тестовых пользователей",
        category="seed", danger="safe",
        description="Создаёт тестовые аккаунты для нагрузочного тестирования (без синтетического матча — см. «Подготовить нагрузочный тест» ниже).",
        args=[
            ArgSpec("--count", "count", "int", default=100, help="Количество пользователей."),
            ArgSpec("--verified", "verified", "flag", help="Создать верифицированных пользователей."),
            ArgSpec("--prefix", "prefix", "str", default="test_user", help="Префикс username."),
        ],
    ),
    "setup_load_test": CommandSpec(
        name="setup_load_test", label="Подготовить нагрузочный тест (Locust)",
        category="seed", danger="safe",
        description="Готовит тестовых пользователей и синтетический матч для Locust-прогона. Очищается командой «Очистить нагрузочный тест» ниже.",
        args=[
            ArgSpec("--users", "users", "int", default=200, help="Сколько тестовых аккаунтов создать."),
        ],
    ),
    "open_voting_for_past_matches": CommandSpec(
        name="open_voting_for_past_matches", label="Открыть голосование для прошедших матчей",
        category="seed", danger="safe",
        description="Открывает окно голосования для уже завершённых матчей — удобно, чтобы сразу протестировать сидирование оценок.",
        args=[
            ArgSpec("--hours", "hours", "int", default=48, help="На сколько часов открыть голосование."),
            ArgSpec("--all", "all", "flag", help="Открыть для ВСЕХ завершённых матчей (игнорирует --days)."),
            ArgSpec("--days", "days", "int", default=7, help="За сколько последних дней брать матчи."),
        ],
    ),
    "simulate_match_timing": CommandSpec(
        name="simulate_match_timing", label="Сдвинуть время/статус матча",
        category="seed", danger="safe",
        description="Двигает существующий матч по времени/статусу — чтобы retention-задачи (напоминания о прогнозе и т.д.) нашли что обработать без ожидания реального крона.",
        args=[
            ArgSpec("match_id", "match_id", "str", positional=True, help="UUID матча. Пусто — команда выведет список последних матчей."),
            ArgSpec("--start-in-minutes", "start_in_minutes", "int", help="Сдвинуть start_time на N минут от сейчас (можно отрицательное)."),
            ArgSpec("--status", "status", "choice", choices=_MATCH_STATUS_CHOICES, help="Новый статус матча."),
            ArgSpec("--home-score", "home_score", "int", help="Счёт хозяев."),
            ArgSpec("--away-score", "away_score", "int", help="Счёт гостей."),
            ArgSpec("--voting-hours", "voting_hours", "int", default=48, help="Если --status finished — на сколько часов открыть голосование."),
            ArgSpec("--release", "release", "flag", help="Снять manual_override — вернуть матч под автосинк Sportmonks, больше ничего не менять."),
        ],
    ),

    # ============================================================
    # ОЧИСТКА / УДАЛЕНИЕ
    # ============================================================
    "cleanup_test_users": CommandSpec(
        name="cleanup_test_users", label="Удалить тестовых пользователей",
        category="cleanup", danger="destructive", has_apply_flag=True,
        description="Удаляет опознанных тестовых пользователей (по префиксу username / домену email) и все их данные. Без --apply — только предпросмотр списка.",
        args=[
            ArgSpec("--prefix", "prefix", "str", default="test_user", help="Префикс username, считающийся тестовым."),
            ArgSpec("--domain", "domain", "str", default="test.dopx.kz", help="Домен email, считающийся тестовым."),
            ArgSpec("--limit-preview", "limit_preview", "int", default=50, help="Сколько строк показывать в dry-run (0 — все)."),
        ],
    ),
    "reset_ratings_data": CommandSpec(
        name="reset_ratings_data", label="Сбросить все оценки и рейтинги",
        category="cleanup", danger="destructive", has_apply_flag=True,
        description="Удаляет ВСЕ оценки (evaluations) и посчитанные из них рейтинги (aggregates) — матчи/команды/игроков/пользователей не трогает. Без --apply — только dry-run подсчёт.",
        args=[
            ArgSpec("--keep-user", "keep_user", "str", help="Username или email — чьи оценки НЕ удалять."),
        ],
    ),
    "clear_player_photos": CommandSpec(
        name="clear_player_photos", label="Удалить фото игроков",
        category="cleanup", danger="destructive", has_apply_flag=True,
        description="Удаляет файлы фото игроков и очищает поле Player.photo — для отката отменённого импорта фото. Без --apply — только отчёт.",
        args=[],
    ),
    "cleanup_load_test": CommandSpec(
        name="cleanup_load_test", label="Очистить нагрузочный тест",
        category="cleanup", danger="destructive", has_apply_flag=False,
        description=(
            "Безусловно удаляет тестовых пользователей и синтетический матч, "
            "созданные «Подготовить нагрузочный тест» — у САМОЙ команды нет "
            "--apply/dry-run, поэтому здесь требуется ручное подтверждение "
            "текстом перед запуском."
        ),
        args=[],
    ),

    # ============================================================
    # ПЕРЕСЧЁТ / ОБСЛУЖИВАНИЕ
    # ============================================================
    "recalculate_aggregates": CommandSpec(
        name="recalculate_aggregates", label="Пересчитать агрегаты матчей",
        category="recalc", danger="safe",
        description="Пересчитывает агрегаты рейтингов для конкретного матча или всех активных за период — полезно сразу после сидирования тестовых оценок.",
        args=[
            ArgSpec("--match-id", "match_id", "str", help="UUID конкретного матча."),
            ArgSpec("--all-active", "all_active", "flag", help="Пересчитать для всех активных матчей."),
            ArgSpec("--hours", "hours", "int", default=24, help="Период в часах для поиска активных матчей."),
        ],
    ),
    "recompute_closed_rounds": CommandSpec(
        name="recompute_closed_rounds", label="Пересчитать закрытые туры (сборные)",
        category="recalc", danger="safe",
        description="Пересчитывает состав уже ЗАКРЫТЫХ (is_final=True) туров без повторной рассылки писем с итогами.",
        args=[
            ArgSpec("--season-id", "season_id", "str", help="UUID сезона. Пусто — все сезоны."),
            ArgSpec("--tour", "tour", "int", help="Только этот номер тура (требует --season-id)."),
        ],
    ),
    "refresh_coach_activity": CommandSpec(
        name="refresh_coach_activity", label="Пересчитать активность тренеров",
        category="recalc", danger="safe",
        description="Пересчитывает is_active у тренеров по последнему сыгранному матчу их команды. Без обращения к внешнему API, выполняется сразу.",
        args=[],
    ),

    # ============================================================
    # ПРОВЕРКА ФИО (ИИ) — 2026-09-22, прямая просьба пользователя после
    # жалобы "Сергий Малий" вместо "Сергий Малый" (см. parsers/name_ai.py
    # и parsers/models.py::NameVerificationSuggestion за полным разбором).
    # ============================================================
    "verify_names_with_ai": CommandSpec(
        name="verify_names_with_ai", label="Проверить ФИО через Gemini (веб-поиск)",
        category="ai_names", danger="safe",
        description=(
            "Находит игроков/судей/тренеров с УГАДАННЫМ (не подтверждённым источником) "
            "написанием ФИО и спрашивает у Gemini API реальное написание через веб-поиск. "
            "НИЧЕГО не меняет напрямую — только кладёт предложения в очередь «Проверка ФИО» "
            "на подтверждение/отклонение. Требует GEMINI_API_KEY в настройках сервера."
        ),
        args=[
            ArgSpec("--all", "all", "flag", help="Разовый прогон по ВСЕМ записям (не только «угадано») — для первого полного прохода по базе."),
            ArgSpec("--entity", "entity", "choice", choices=["player", "referee", "coach"], help="Ограничиться одним типом. Пусто — все три."),
            ArgSpec("--limit", "limit", "int", default=20, help="Максимум вызовов Gemini за запуск (бережём бесплатный лимит). 0 — без ограничения, проверит всех кандидатов."),
            ArgSpec("--delay", "delay", "float", default=4.0, help="Пауза в секундах между вызовами Gemini."),
            ArgSpec("--recheck", "recheck", "flag", help="Не пропускать записи, у которых уже есть предложение (любого статуса)."),
            ArgSpec("--dry-run", "dry_run", "flag", help="Только показать список кандидатов, не тратить вызовы Gemini."),
        ],
    ),

    # ============================================================
    # ДИАГНОСТИКА (read-only)
    # ============================================================
    "diagnose_match_events": CommandSpec(
        name="diagnose_match_events", label="Диагностика: события матча",
        category="diagnose", danger="readonly",
        description="Печатает сырой extra_data всех событий подходящего матча. Ничего не меняет.",
        args=[
            ArgSpec("teams", "teams", "list_str", positional=True, required=True, help="Слова из названий команд, по одному на строку (например: Кайрат / Тобыл)."),
            ArgSpec("--year", "year", "int", help="Год матча."),
            ArgSpec("--limit", "limit", "int", default=5, help="Сколько последних подходящих матчей показать."),
        ],
    ),
    "diagnose_nominations": CommandSpec(
        name="diagnose_nominations", label="Диагностика: номинации сезона",
        category="diagnose", danger="readonly",
        description="Проверяет витрину «Номинации сезона» на главной — расхождение с версией на странице лиги, битые ссылки на игроков. Ничего не меняет.",
        args=[],
    ),
    "diagnose_matches_played": CommandSpec(
        name="diagnose_matches_played", label="Диагностика: сыгранные матчи команды",
        category="diagnose", danger="readonly",
        description="Сравнивает число сыгранных матчей по разным источникам для одной команды. Ничего не меняет.",
        args=[
            ArgSpec("--team", "team", "str", help="Название команды (частичное совпадение)."),
            ArgSpec("--team-id", "team_id", "str", help="UUID команды."),
        ],
    ),
    "diagnose_team_roster": CommandSpec(
        name="diagnose_team_roster", label="Диагностика: состав команды",
        category="diagnose", danger="readonly",
        description="Почему конкретный игрок есть/нет в текущем составе команды. Ничего не меняет.",
        args=[
            ArgSpec("team", "team", "str", positional=True, required=True, help="Название команды (частичное совпадение) или её id."),
        ],
    ),
}


def get_command(name: str) -> CommandSpec | None:
    return COMMAND_REGISTRY.get(name)


def categories() -> list[tuple[str, str, list[CommandSpec]]]:
    """[(category_key, category_label, [CommandSpec, ...]), ...] в порядке
    CATEGORY_LABELS — для рендера страницы по секциям."""
    result = []
    for key, label in CATEGORY_LABELS.items():
        specs = [spec for spec in COMMAND_REGISTRY.values() if spec.category == key]
        result.append((key, label, specs))
    return result
