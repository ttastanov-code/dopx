# dashboard/commands_registry.py
"""Allowlist management-команд для «Скриптов и команд».
Только команды из COMMAND_REGISTRY и только описанные аргументы.

danger:
  readonly    — ничего не меняет;
  safe        — меняет, но безопасно/идемпотентно;
  destructive — удаляет. Обычно есть --apply (без него — dry-run);
                у cleanup_load_test вместо этого подтверждение текстом.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from matches.models import Match
from notifications.services import PUSH_PROFILES

_MATCH_STATUS_CHOICES = [choice[0] for choice in Match.STATUS_CHOICES]


@dataclass(frozen=True)
class ArgSpec:
    flag: str  # "--season-id" или имя позиционного ("team")
    dest: str  # kwarg для call_command()
    kind: str  # "str" | "int" | "float" | "flag" | "choice" | "list_str"
    positional: bool = False
    required: bool = False
    default: Any = None
    help: str = ""
    choices: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CommandSpec:
    name: str  # имя команды для call_command()
    label: str
    category: str  # "seed" | "cleanup" | "recalc" | "diagnose"
    danger: str  # "readonly" | "safe" | "destructive"
    description: str
    args: list[ArgSpec] = field(default_factory=list)
    has_apply_flag: bool = False  # есть свой --apply (dry-run по умолчанию)
    # Если задано — запуск режется на порции такого размера.
    auto_chunk_limit: int | None = None
    # Права на данные, которые скрипт меняет (кроме readonly); без них запуск закрыт даже с разделом «Скрипты».
    perms: tuple[str, ...] = ()


# Команды, которые требуют вписать своё имя (для остальных опасных хватает галочки apply).
CONFIRM_TEXT_COMMANDS = {"cleanup_load_test", "reset_user_activity"}

# Заголовки секций.
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
        perms=('users.add_user', 'evaluations.change_evaluationsession'),
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
        perms=('users.add_user', 'evaluations.change_evaluationsession'),
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
        perms=('users.add_user', 'evaluations.change_evaluationsession'),
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
        perms=('users.add_user', 'evaluations.change_evaluationsession'),
        category="seed", danger="safe",
        description="Создаёт оценки для одного матча.",
        args=[
            ArgSpec("--match-id", "match_id", "str", required=True, help="UUID матча."),
            ArgSpec("--users", "users", "int", default=5, help="Количество пользователей."),
        ],
    ),
    "create_test_users": CommandSpec(
        name="create_test_users", label="Тестовые пользователи",
        perms=('users.add_user',),
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
        perms=('users.add_user', 'matches.change_match'),
        category="seed", danger="safe",
        description="Тестовые пользователи и синтетический матч для Locust.",
        args=[
            ArgSpec("--users", "users", "int", default=200, help="Количество аккаунтов."),
        ],
    ),
    "open_voting_for_past_matches": CommandSpec(
        name="open_voting_for_past_matches", label="Открыть голосование задним числом",
        perms=('matches.change_match',),
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
        perms=('matches.change_match',),
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
        name="cleanup_test_users", label="Удалить ботов и тестовые аккаунты",
        category="cleanup", danger="destructive", has_apply_flag=True,
        description="Удаляет ботов/тестовые аккаунты со всеми их данными и пересчитывает затронутые рейтинги, "
                    "сборные и туры. Без «Реально применить» — только список.",
        args=[
            ArgSpec("--no-recalc", "no_recalc", "flag", help="Не пересчитывать рейтинги (сделать потом)."),
            ArgSpec("--recalc-all", "recalc_all", "flag",
                    help="Пересчитать все матчи с рейтингами, сборные и туры (если пересчёт прервался)."),
            ArgSpec("--reset-corrections", "reset_corrections", "flag",
                    help="Обнулить авто-поправки и снять необработанные сигналы по сущностям (посчитаны и по голосам ботов)."),
            ArgSpec("--limit-preview", "limit_preview", "int", default=50, help="Строк в предпросмотре (0 — все)."),
        ],
    ),
    "reset_user_activity": CommandSpec(
        name="reset_user_activity", label="Чистый старт: удалить всю активность",
        category="cleanup", danger="destructive", has_apply_flag=True,
        description="Удаляет аккаунты (кроме сотрудников), все оценки, рейтинги, сборные, прогнозы, достижения, "
                    "уведомления, аналитику и вовлечение. У сотрудников обнуляет XP и серии. Матчи, игроков, "
                    "статистику Sportmonks и правки ФИО (ИИ) не трогает. Для применения — вписать имя команды.",
        args=[
            ArgSpec("--keep-user", "keep_user", "list_str", help="Логины или email, чьи аккаунты оставить, по одному на строку."),
            ArgSpec("--delete-staff", "delete_staff", "flag", help="Удалить и сотрудников (потом createsuperuser)."),
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
        description="Удаляет тестовых пользователей и синтетический матч нагрузочного теста. Удаляет сразу — нужно подтверждение текстом.",
        args=[],
    ),

    # ============================================================
    # ПЕРЕСЧЁТ / ОБСЛУЖИВАНИЕ
    # ============================================================
    "recalculate_aggregates": CommandSpec(
        name="recalculate_aggregates", label="Пересчитать агрегаты матчей",
        perms=('matches.change_match',),
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
        perms=('matches.change_match',),
        category="recalc", danger="safe",
        description="Пересчёт состава уже закрытых туров без повторной рассылки писем.",
        args=[
            ArgSpec("--season-id", "season_id", "str", help="UUID сезона (пусто — все)."),
            ArgSpec("--tour", "tour", "int", help="Номер тура (нужен --season-id)."),
        ],
    ),
    "refresh_coach_activity": CommandSpec(
        name="refresh_coach_activity", label="Пересчитать активность тренеров",
        perms=('matches.change_match',),
        category="recalc", danger="safe",
        description="Обновляет «активен» по последнему матчу команды. Без обращения к внешнему API.",
        args=[],
    ),
    "recompute_user_progress": CommandSpec(
        name="recompute_user_progress", label="Пересчитать XP, серии и достижения",
        perms=('users.change_user',),
        category="recalc", danger="safe", has_apply_flag=True,
        description="Из фактических данных: завершённые оценки и прогнозы. Нужно после удаления сессий/прогнозов вручную.",
        args=[ArgSpec("--user", "user", "str", help="username; пусто — все пользователи")],
    ),
    "sync_sportmonks_photos": CommandSpec(
        name="sync_sportmonks_photos", label="Догрузить фото из Sportmonks",
        perms=('players.change_player',),
        category="recalc", danger="safe", has_apply_flag=True,
        description="Фото игроков (по составам сезона), судей и тренеров. Заглушки пропускает. Обращается к API Sportmonks.",
        args=[ArgSpec("--entity", "entity", "choice", default="all", choices=["all", "players", "referees", "coaches"])],
    ),

    # ============================================================
    # ПРОВЕРКА ФИО (ИИ)
    # ============================================================
    "verify_names_with_ai": CommandSpec(
        name="verify_names_with_ai", label="Проверить ФИО через ИИ",
        perms=('parsers.change_nameverificationsuggestion',),
        category="ai_names", danger="safe",
        description="Ищет реальное написание ФИО через веб-поиск. Не меняет ничего напрямую — кладёт предложения в очередь на подтверждение. Нужен GEMINI_API_KEY.",
        # Порции по 40 кандидатов — долгие прогоны падали по OOM.
        auto_chunk_limit=40,
        args=[
            ArgSpec("--all", "all", "flag", help="Прогон по всем записям, не только угаданным."),
            ArgSpec("--entity", "entity", "choice", choices=["player", "referee", "coach"], help="Один тип (пусто — все три)."),
            ArgSpec("--limit", "limit", "int", default=20, help="Не действует из дашборда — прогон всегда режется на порции по 40."),
            ArgSpec("--delay", "delay", "float", default=4.0, help="Пауза между вызовами ИИ, сек."),
            ArgSpec("--recheck", "recheck", "flag", help="Проверить и уже разобранные записи."),
            ArgSpec("--dry-run", "dry_run", "flag", help="Только список кандидатов, без вызовов ИИ."),
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
    "send_test_push": CommandSpec(
        name="send_test_push", label="Тестовый push",
        perms=('notifications.add_notification',),
        category="diagnose", danger="safe",
        description="Шлёт пример уведомления выбранного типа (или всех типов) на все устройства пользователя "
                    "и показывает результат. Выключенные пользователем типы не придут — как в бою.",
        args=[
            ArgSpec("username", "username", "str", positional=True, required=True, help="Логин пользователя."),
            ArgSpec("--kind", "kind", "choice", default="match_event", choices=sorted(PUSH_PROFILES),
                    help="Тип уведомления."),
            ArgSpec("--all-kinds", "all_kinds", "flag", help="Отправить по одному каждого типа."),
        ],
    ),
    "publish_weekly_poll": CommandSpec(
        name="publish_weekly_poll", label="Опрос недели сейчас",
        perms=('notifications.add_notification',),
        category="diagnose", danger="safe", has_apply_flag=True,
        description="«Спорный момент» или «Дуэль тура» вне расписания (по расписанию — вт и ср в 12:00). "
                    "Без «Реально применить» — покажет, какой эпизод или пара будут выбраны.",
        args=[
            ArgSpec("--kind", "kind", "choice", default="episode", choices=["episode", "duel"], help="Тип опроса."),
            ArgSpec("--no-push", "no_push", "flag", help="Без push активным пользователям."),
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
    # ============================================================
    # РЕЙТИНГИ / СТАТИСТИКА МАТЧЕЙ
    # ============================================================
    "sportmonks_backfill_stats": CommandSpec(
        name="sportmonks_backfill_stats", label="Догрузить полную статистику прошлых матчей",
        perms=('matches.change_match',),
        category="recalc", danger="safe",
        description="Для уже сыгранных матчей подтягивает всю статистику игроков и команд (оценка по статистике, отборы, перехваты и т.д.). 1 запрос к API на матч, счёт и составы не трогает.",
        args=[
            ArgSpec("--season-only", "season_only", "flag", help="Только текущий сезон."),
            ArgSpec("--force", "force", "flag", help="Перезалить и матчи, где полная статистика уже есть."),
            ArgSpec("--limit", "limit", "int", default=0, help="Не больше N матчей (0 — все)."),
        ],
    ),
    "recalculate_history_clean": CommandSpec(
        name="recalculate_history_clean", label="Пересчитать историю рейтингов без авто-поправок",
        category="recalc", danger="destructive", has_apply_flag=True,
        description="Пересчитывает рейтинги игроков и команд по всем завершённым матчам без старых поправок защиты от накрутки — чистая оценка болельщиков. Новые матчи считаются как обычно. Без «Применить» — только показывает число матчей.",
    ),
    "calibrate_player_objective_weights": CommandSpec(
        name="calibrate_player_objective_weights", label="Подобрать веса формулы «по статистике»",
        perms=('core.change_platformsetting',),
        category="recalc", danger="safe",
        description="Подбирает по данным веса запасной формулы оценки игры (когда у матча нет готовой оценки по статистике) и показывает точность. С флагом «сохранить» — записывает в «Настройки платформы».",
        args=[
            ArgSpec("--save", "save", "flag", help="Сохранить веса в «Настройки платформы»."),
        ],
    ),
    "audit_data": CommandSpec(
        name="audit_data", label="Проверка целостности данных",
        category="diagnose", danger="readonly",
        description="Сверяет счёт с событиями, таблицу с результатами, голоса в рейтингах с засчитанными, уровни, серии, составы и сборные. Ничего не меняет.",
    ),
    "sportmonks_inspect_stats": CommandSpec(
        name="sportmonks_inspect_stats", label="Какая статистика приходит от поставщика данных",
        category="diagnose", danger="readonly",
        description="1 запрос по последнему завершённому матчу: список всех видов статистики игроков по амплуа и команд. В базу ничего не пишет, полный ответ сохраняет в файл.",
        args=[
            ArgSpec("--fixture", "fixture", "int", help="ID матча у поставщика (пусто — последний завершённый)."),
        ],
    ),
}


def _is_available(spec: CommandSpec) -> bool:
    """Сиды и симуляции подделывают голоса — вне ALLOW_SEED_COMMANDS их нет."""
    from django.conf import settings

    return spec.category != "seed" or getattr(settings, "ALLOW_SEED_COMMANDS", False)


def get_command(name: str) -> CommandSpec | None:
    spec = COMMAND_REGISTRY.get(name)
    return spec if spec is not None and _is_available(spec) else None


def categories() -> list[tuple[str, str, list[CommandSpec]]]:
    """[(ключ, подпись, [CommandSpec, ...]), ...] в порядке CATEGORY_LABELS."""
    result = []
    for key, label in CATEGORY_LABELS.items():
        specs = [spec for spec in COMMAND_REGISTRY.values() if spec.category == key and _is_available(spec)]
        if specs:
            result.append((key, label, specs))
    return result
