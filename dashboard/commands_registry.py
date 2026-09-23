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

`label`/`description`/`ArgSpec.help` — ТЕКСТ ДЛЯ STAFF В UI (scripts.html).
2026-09-22, прямая просьба пользователя: "все описания команд надо
переписать на более короткие, емкие, понятные и без ИИ паттерна" —
короткое предложение по делу, БЕЗ обоснований "почему это нужно"/истории
решений (та история и так есть рядом в виде Python-комментариев в этом
файле — staff, читающий кнопку в браузере, её не видит и не должен, ему
нужно только "что делает").

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
    # 2026-09-22, прямая просьба пользователя после повторных зависаний
    # verify_names_with_ai --all --limit 0 (см. её докстринг ниже и
    # dashboard/tasks.py::run_management_command): если задано — реальный
    # вызов ВСЕГДА режется на порции по auto_chunk_limit штук вместо
    # одного многочасового синхронного прохода, независимо от того, что
    # staff ввёл в поле --limit формы. Один упавший/убитый OOM'ом чанк
    # теряет минуты работы, а не часы, и не оставляет запись "зависшей"
    # навсегда — дедупликация внутри самой команды делает продолжение
    # автоматическим.
    auto_chunk_limit: int | None = None


# 2026-09-22: короче исходных ("Сидирование и симуляция тестовых данных" и
# т.п.) — те же длинные подписи, что жаловался пользователь у самих команд,
# только на уровень выше (заголовок секции на scripts.html).
CATEGORY_LABELS = {
    "seed": "Тестовые данные",
    "cleanup": "Удаление",
    "recalc": "Пересчёт",
    "ai_names": "Проверка ФИО (ИИ)",
    "diagnose": "Диагностика",
}

COMMAND_REGISTRY: dict[str, CommandSpec] = {
    # ============================================================
    # СИДИРОВАНИЕ / СИМУЛЯЦИЯ
    # ============================================================
    "seed_full_history": CommandSpec(
        name="seed_full_history", label="Полная история сезона",
        category="seed", danger="safe", has_apply_flag=False,
        description="Оценки, прогнозы, XP, бейджи и сборные тура/сезона по тестовым ботам. Повтор не плодит дубли.",
        args=[
            ArgSpec("--season-id", "season_id", "str", help="UUID сезона (пусто — активный)."),
            ArgSpec("--tour-from", "tour_from", "int", help="С какого тура."),
            ArgSpec("--tour-to", "tour_to", "int", help="По какой тур."),
            ArgSpec("--pool-size", "pool_size", "int", default=150, help="Размер пула ботов."),
            ArgSpec("--seed", "seed", "int", help="Seed для повторяемого результата."),
            ArgSpec("--no-recalc", "no_recalc", "flag", help="Не пересчитывать агрегаты и таблицу."),
            ArgSpec("--no-badges", "no_badges", "flag", help="Не проверять достижения."),
        ],
    ),
    "seed_match_votes": CommandSpec(
        name="seed_match_votes", label="Голоса за матчи",
        category="seed", danger="safe",
        description="Точечное голосование ботов за выбранные матчи.",
        args=[
            ArgSpec("--match-id", "match_ids", "list_str", help="UUID матчей, по одному на строку."),
            ArgSpec("--status", "status", "str", help="Или статус матчей вместо списка id."),
            ArgSpec("--limit", "limit", "int", default=10, help="Сколько матчей брать по статусу."),
            ArgSpec("--voters", "voters", "int", default=20, help="Ботов на один матч."),
            ArgSpec("--pool-size", "pool_size", "int", default=300, help="Размер пула ботов."),
            ArgSpec("--player-coverage", "player_coverage", "float", default=0.7, help="Доля голосующих на игрока (0–1)."),
            ArgSpec("--single-inflated-player", "single_inflated_player", "str", help="UUID игрока для экстремального голоса (тест антифрода)."),
            ArgSpec("--seed", "seed", "int", help="Seed для повторяемого результата."),
            ArgSpec("--no-recalc", "no_recalc", "flag", help="Не пересчитывать агрегаты."),
        ],
    ),
    "simulate_evaluations": CommandSpec(
        name="simulate_evaluations", label="Оценки пользователей",
        category="seed", danger="safe",
        description="Симулирует оценки от нескольких пользователей сразу по матчам.",
        args=[
            ArgSpec("--users", "users", "int", default=50, help="Количество пользователей."),
            ArgSpec("--matches", "matches", "int", default=5, help="Количество матчей."),
            ArgSpec("--match-ids", "match_ids", "str", help="ID матчей через запятую (вместо --matches)."),
            ArgSpec("--recalculate", "recalculate", "flag", help="Пересчитать агрегаты после."),
        ],
    ),
    "create_test_evaluations": CommandSpec(
        name="create_test_evaluations", label="Тестовые оценки для матча",
        category="seed", danger="safe",
        description="Создаёт оценки для одного матча.",
        args=[
            ArgSpec("--match-id", "match_id", "str", required=True, help="UUID матча."),
            ArgSpec("--users", "users", "int", default=5, help="Количество пользователей."),
        ],
    ),
    "create_test_users": CommandSpec(
        name="create_test_users", label="Тестовые пользователи",
        category="seed", danger="safe",
        description="Создаёт тестовые аккаунты.",
        args=[
            ArgSpec("--count", "count", "int", default=100, help="Количество."),
            ArgSpec("--verified", "verified", "flag", help="Сделать верифицированными."),
            ArgSpec("--prefix", "prefix", "str", default="test_user", help="Префикс username."),
        ],
    ),
    "setup_load_test": CommandSpec(
        name="setup_load_test", label="Подготовить нагрузочный тест",
        category="seed", danger="safe",
        description="Тестовые пользователи и синтетический матч для Locust.",
        args=[
            ArgSpec("--users", "users", "int", default=200, help="Количество аккаунтов."),
        ],
    ),
    "open_voting_for_past_matches": CommandSpec(
        name="open_voting_for_past_matches", label="Открыть голосование задним числом",
        category="seed", danger="safe",
        description="Открывает окно голосования для прошедших матчей.",
        args=[
            ArgSpec("--hours", "hours", "int", default=48, help="На сколько часов открыть."),
            ArgSpec("--all", "all", "flag", help="Для всех завершённых матчей."),
            ArgSpec("--days", "days", "int", default=7, help="За сколько дней брать матчи."),
        ],
    ),
    "simulate_match_timing": CommandSpec(
        name="simulate_match_timing", label="Сдвинуть время/статус матча",
        category="seed", danger="safe",
        description="Меняет время или статус существующего матча — для теста напоминаний без ожидания крона.",
        args=[
            ArgSpec("match_id", "match_id", "str", positional=True, help="UUID матча (пусто — покажет список последних)."),
            ArgSpec("--start-in-minutes", "start_in_minutes", "int", help="Сдвиг старта в минутах (можно отрицательный)."),
            ArgSpec("--status", "status", "choice", choices=_MATCH_STATUS_CHOICES, help="Новый статус матча."),
            ArgSpec("--home-score", "home_score", "int", help="Счёт хозяев."),
            ArgSpec("--away-score", "away_score", "int", help="Счёт гостей."),
            ArgSpec("--voting-hours", "voting_hours", "int", default=48, help="Часы голосования, если статус finished."),
            ArgSpec("--release", "release", "flag", help="Вернуть матч под автосинк Sportmonks."),
        ],
    ),

    # ============================================================
    # ОЧИСТКА / УДАЛЕНИЕ
    # ============================================================
    "cleanup_test_users": CommandSpec(
        name="cleanup_test_users", label="Удалить тестовых пользователей",
        category="cleanup", danger="destructive", has_apply_flag=True,
        description="Удаляет тестовые аккаунты и их данные. Без «Реально применить» — только список.",
        args=[
            ArgSpec("--prefix", "prefix", "str", default="test_user", help="Префикс username."),
            ArgSpec("--domain", "domain", "str", default="test.dopx.kz", help="Домен email."),
            ArgSpec("--limit-preview", "limit_preview", "int", default=50, help="Строк в предпросмотре (0 — все)."),
        ],
    ),
    "reset_ratings_data": CommandSpec(
        name="reset_ratings_data", label="Сбросить оценки и рейтинги",
        category="cleanup", danger="destructive", has_apply_flag=True,
        description="Удаляет все оценки и посчитанные рейтинги. Матчи, команды, игроков не трогает.",
        args=[
            ArgSpec("--keep-user", "keep_user", "str", help="Username или email — чьи оценки не удалять."),
        ],
    ),
    "clear_player_photos": CommandSpec(
        name="clear_player_photos", label="Удалить фото игроков",
        category="cleanup", danger="destructive", has_apply_flag=True,
        description="Удаляет файлы фото и очищает поле у игроков.",
        args=[],
    ),
    "cleanup_load_test": CommandSpec(
        name="cleanup_load_test", label="Очистить нагрузочный тест",
        category="cleanup", danger="destructive", has_apply_flag=False,
        description="Удаляет тестовых пользователей и синтетический матч нагрузочного теста. Без dry-run — нужно подтверждение текстом.",
        args=[],
    ),

    # ============================================================
    # ПЕРЕСЧЁТ / ОБСЛУЖИВАНИЕ
    # ============================================================
    "recalculate_aggregates": CommandSpec(
        name="recalculate_aggregates", label="Пересчитать агрегаты матчей",
        category="recalc", danger="safe",
        description="Пересчитывает рейтинги — для одного матча или всех активных.",
        args=[
            ArgSpec("--match-id", "match_id", "str", help="UUID конкретного матча."),
            ArgSpec("--all-active", "all_active", "flag", help="Все активные матчи."),
            ArgSpec("--hours", "hours", "int", default=24, help="Период поиска активных, часы."),
        ],
    ),
    "recompute_closed_rounds": CommandSpec(
        name="recompute_closed_rounds", label="Пересчитать закрытые туры",
        category="recalc", danger="safe",
        description="Пересчёт состава уже закрытых туров без повторной рассылки писем.",
        args=[
            ArgSpec("--season-id", "season_id", "str", help="UUID сезона (пусто — все)."),
            ArgSpec("--tour", "tour", "int", help="Номер тура (нужен --season-id)."),
        ],
    ),
    "refresh_coach_activity": CommandSpec(
        name="refresh_coach_activity", label="Пересчитать активность тренеров",
        category="recalc", danger="safe",
        description="Обновляет «активен» по последнему матчу команды. Без обращения к внешнему API.",
        args=[],
    ),

    # ============================================================
    # ПРОВЕРКА ФИО (ИИ) — 2026-09-22, прямая просьба пользователя после
    # жалобы "Сергий Малий" вместо "Сергий Малый" (см. parsers/name_ai.py
    # и parsers/models.py::NameVerificationSuggestion за полным разбором).
    # ============================================================
    "verify_names_with_ai": CommandSpec(
        name="verify_names_with_ai", label="Проверить ФИО через Gemini",
        category="ai_names", danger="safe",
        description="Ищет реальное написание ФИО через веб-поиск. Не меняет ничего напрямую — кладёт предложения в очередь на подтверждение. Нужен GEMINI_API_KEY.",
        # 2026-09-22: большие прогоны (--all --limit 0) несколько раз
        # зависали и умирали часами позже (OOM/SIGKILL одного из форкнутых
        # процессов воркера — см. историю в чате) без возможности
        # продолжить. auto_chunk_limit=40 автоматически режет ЛЮБОЙ запуск
        # этой команды из дашборда на куски по 40 кандидатов (~7 минут при
        # --delay 10) — что бы staff ни ввёл в поле --limit ниже.
        auto_chunk_limit=40,
        args=[
            ArgSpec("--all", "all", "flag", help="Прогон по всем записям, не только угаданным."),
            ArgSpec("--entity", "entity", "choice", choices=["player", "referee", "coach"], help="Один тип (пусто — все три)."),
            ArgSpec("--limit", "limit", "int", default=20, help="Не действует из дашборда — прогон всегда режется на порции по 40."),
            ArgSpec("--delay", "delay", "float", default=4.0, help="Пауза между вызовами Gemini, сек."),
            ArgSpec("--recheck", "recheck", "flag", help="Проверить и уже разобранные записи."),
            ArgSpec("--dry-run", "dry_run", "flag", help="Только список кандидатов, без вызовов Gemini."),
        ],
    ),

    # ============================================================
    # ДИАГНОСТИКА (read-only)
    # ============================================================
    "diagnose_match_events": CommandSpec(
        name="diagnose_match_events", label="События матча",
        category="diagnose", danger="readonly",
        description="Печатает сырые данные событий подходящего матча.",
        args=[
            ArgSpec("teams", "teams", "list_str", positional=True, required=True, help="Слова из названий команд, по одному на строку."),
            ArgSpec("--year", "year", "int", help="Год матча."),
            ArgSpec("--limit", "limit", "int", default=5, help="Сколько матчей показать."),
        ],
    ),
    "diagnose_nominations": CommandSpec(
        name="diagnose_nominations", label="Номинации сезона",
        category="diagnose", danger="readonly",
        description="Проверяет витрину номинаций на расхождения и битые ссылки.",
        args=[],
    ),
    "diagnose_matches_played": CommandSpec(
        name="diagnose_matches_played", label="Сыгранные матчи команды",
        category="diagnose", danger="readonly",
        description="Сравнивает число сыгранных матчей по разным источникам.",
        args=[
            ArgSpec("--team", "team", "str", help="Название команды (частичное совпадение)."),
            ArgSpec("--team-id", "team_id", "str", help="UUID команды."),
        ],
    ),
    "diagnose_team_roster": CommandSpec(
        name="diagnose_team_roster", label="Состав команды",
        category="diagnose", danger="readonly",
        description="Почему игрок есть или нет в текущем составе команды.",
        args=[
            ArgSpec("team", "team", "str", positional=True, required=True, help="Название (частичное совпадение) или id команды."),
        ],
    ),
    "diagnose_duplicate_players": CommandSpec(
        name="diagnose_duplicate_players", label="Дубли игроков",
        category="diagnose", danger="readonly",
        description="Ищет игроков с одинаковым ФИО в одной команде и разным sportmonks_id. Решение — через очередь «Дубли игроков».",
        args=[],
    ),
    "merge_duplicate_players": CommandSpec(
        name="merge_duplicate_players", label="Объединить дубль игрока (по id)",
        category="cleanup", danger="destructive", has_apply_flag=True,
        description="Переносит статистику с одной записи на другую и удаляет донора. Обычно проще через очередь «Дубли игроков» — это для разового разбора по id из терминала.",
        args=[
            ArgSpec("--keep", "keep", "str", required=True, help="UUID записи, которую оставляем."),
            ArgSpec("--merge", "merge", "str", required=True, help="UUID записи, которую сливаем и удаляем."),
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
