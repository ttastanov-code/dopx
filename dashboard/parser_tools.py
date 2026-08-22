# dashboard/parser_tools.py
"""
Ручное управление KFF-парсером из staff-панели: сырые ответы API
(raw_kff_response), точечный ресинк матча (resync_match), запуск
celery-задач с дебаунсом (TRIGGERABLE_TASKS), поиск матча по названию
команд (search_matches), health-check API и инспекция/отмена celery-задач.
"""
from __future__ import annotations

import json
import logging
import time

from django.core.cache import cache

from core.utils import normalize_kz
from parsers.kff.client import KFFClient

logger = logging.getLogger(__name__)

RAW_ENDPOINTS = {
    "game": ("Детали матча", lambda client, ext_id, tournament: client.get_game_details(ext_id, tournament_code=tournament)),
    "events": ("События", lambda client, ext_id, tournament: client.get_events(ext_id, tournament_code=tournament)),
    "lineup": ("Составы", lambda client, ext_id, tournament: client.get_lineup(ext_id, tournament_code=tournament)),
    "stats": ("Статистика", lambda client, ext_id, tournament: client.get_stats(ext_id, tournament_code=tournament)),
}


def raw_kff_response(external_id: int, endpoint: str, tournament_code: str = "pl") -> dict:
    """Синхронный вызов — страница staff-тулинга, не celery. Приемлемо
    заблокировать один HTTP-запрос staff-сотрудника на пару секунд ради
    того, чтобы не городить отдельный async-опрос результата."""
    if endpoint not in RAW_ENDPOINTS:
        return {"error": f"Неизвестный эндпоинт: {endpoint}"}

    label, fn = RAW_ENDPOINTS[endpoint]
    client = KFFClient()
    try:
        data = fn(client, external_id, tournament_code)
    except Exception as e:
        logger.error(f"raw_kff_response({external_id}, {endpoint}): {e}", exc_info=True)
        return {"error": f"{type(e).__name__}: {e}"}

    if data is None:
        return {"error": "API вернул пустой ответ (None) — проверьте external_id и tournament_code"}
    return {"label": label, "data": data, "pretty": json.dumps(data, ensure_ascii=False, indent=2)}


def resync_match(match) -> tuple[bool, str]:
    """Полный ресинк ОДНОГО матча (delete+recreate событий/составов из
    свежего ответа API — см. parsers/kff/importers.py::import_full_match).
    Синхронный вызов: staff жмёт кнопку и сразу видит результат, не ждёт
    celery beat следующего цикла."""
    from parsers.kff.pipeline import import_full_match

    if not match.external_id:
        return False, "У матча нет external_id — синхронизация невозможна"

    try:
        success = import_full_match(match.external_id, tournament_code="pl")
    except Exception as e:
        logger.error(f"resync_match({match.id}): {e}", exc_info=True)
        return False, f"{type(e).__name__}: {e}"

    if success:
        return True, f"Матч {match} пересинхронизирован"
    return False, "import_full_match вернул False — подробности в логах сервера"


# ============================================================
# Ручной запуск celery-задач с дебаунсом
# ============================================================

TASK_DEBOUNCE_SECONDS = 60

# sync_full_season сюда намеренно не включена — тяжёлая операция на весь
# сезон, риск случайного клика слишком высок; для полного пересинка есть
# отдельная management-команда с сервера, не кнопка в UI.
TRIGGERABLE_TASKS = {
    "update_match_statuses": "Обновить статусы/события live- и scheduled-матчей",
    "sync_kff_premier_league": "Полный синк текущего сезона Премьер-Лиги",
    "sync_recent_matches": "Досинхронизировать последние завершённые матчи",
    "check_sync_errors_and_alert": "Проверить ошибки синка за 24ч и отправить алерт при превышении порога",
    "sync_all_enabled_tournaments": "Синхронизировать все включённые турниры (PARSER_SETTINGS)",
    # Retention loops (2026-08-21) — ручной прогон для тестирования без
    # ожидания реального крон-тика (см. core/management/commands/
    # simulate_match_timing.py — двигает существующий матч по времени,
    # чтобы эти задачи нашли что обработать, затем жмём кнопку здесь).
    "notify_prediction_closing_soon": "Прогнозы: приглашение за час до старта (push+email+in-app)",
    "notify_prediction_results": "Прогнозы: «ваш прогноз vs результат» по завершённым матчам",
    "send_weekly_summary": "Персональная недельная сводка активности",
    # "Живая сборная сезона" (2026-08-21) — ручной пересчёт вне 15-минутного
    # крон-тика, полезно сразу после массового прогона оценок в тестах.
    "recompute_all_active_best_xi": "Сборная DOPX: пересчитать сейчас (все активные сезоны)",
    # "DOPX Лучшие тура" (2026-08-22) — тот же принцип, что и у сборной
    # сезона выше: не ждать 15-минутный крон-тик, полезно сразу после
    # прогона тестовых голосований (see users/management/commands/
    # create_test_users.py + aggregates/management/commands/simulate_evaluations.py).
    "recompute_active_rounds": "DOPX Лучшие тура: пересчитать сейчас (все незакрытые туры)",
}

# Короткие пояснения под кнопками (2026-08-21, продуктовый фидбек: "не
# всегда понятно, что именно выполняют") — что реально делает задача,
# откуда берёт данные и когда результат будет заметен на сайте. Ключи
# СТРОГО совпадают с TRIGGERABLE_TASKS — тест dashboard/tests.py при
# желании может проверить это множествами, здесь не форсируем ради
# простоты словаря.
TASK_DESCRIPTIONS: dict[str, str] = {
    "update_match_statuses": (
        "Опрашивает KFF API по live- и scheduled-матчам: обновляет счёт, "
        "минуту, статус матча и события (голы/карточки/замены). Быстрая, "
        "безопасно жать в любой момент."
    ),
    "sync_kff_premier_league": (
        "Тяжёлая операция — перекачивает ВЕСЬ текущий сезон Премьер-Лиги с "
        "нуля (команды, игроки, все матчи и составы). Может занять минуты; "
        "нужна редко — например, после ручных правок в KFF или подозрения "
        "на массовый рассинхрон."
    ),
    "sync_recent_matches": (
        "Точечно досинхронизирует только последние завершившиеся матчи "
        "(по умолчанию 10) — быстрее полного синка, для рутинного "
        "'подтянуть свежие результаты'."
    ),
    "check_sync_errors_and_alert": (
        "Смотрит на ParserSyncRun за последние 24 часа — если ошибок "
        "больше порога, шлёт email-алерт админу. Ничего не синхронизирует "
        "и не меняет данные, только диагностика."
    ),
    "sync_all_enabled_tournaments": (
        "То же, что полный синк Премьер-Лиги, но сразу по ВСЕМ турнирам из "
        "PARSER_SETTINGS['ENABLED_TOURNAMENTS'] в dopx/settings.py (сейчас "
        "включена только Премьер-Лига — 'pl'). Тяжёлая, редкая операция."
    ),
    "notify_prediction_closing_soon": (
        "Рассылает push/email/in-app приглашение сделать прогноз тем, у "
        "кого матч стартует в течение часа, а прогноза ещё нет."
    ),
    "notify_prediction_results": (
        "По завершённым матчам рассылает уведомление 'ваш прогноз vs "
        "результат' тем, кто делал прогноз на этот матч."
    ),
    "send_weekly_summary": (
        "Формирует и рассылает всем активным пользователям персональную "
        "сводку активности за неделю (оценки, XP, достижения)."
    ),
    "recompute_all_active_best_xi": (
        "Пересчитывает «Сборную DOPX» (/season/best-xi/) по всем активным "
        "сезонам прямо сейчас, не дожидаясь крон-тика раз в 15 минут. "
        "ВАЖНО: слот остаётся пустым, пока у кандидатов в нём МЕНЬШЕ 2 "
        "оценённых матчей в сезоне (см. season_squad/services.py::"
        "MIN_MATCHES_FOR_CANDIDATE) — это не баг задачи, а осознанный "
        "порог, чтобы один матч с оценкой 10/10 не выносил игрока в топ."
    ),
    "recompute_active_rounds": (
        "Находит все туры активных сезонов с хотя бы одним завершённым "
        "матчем и ещё не зафиксированным составом (/season/round/) и "
        "пересчитывает их прямо сейчас, не дожидаясь крон-тика раз в 15 "
        "минут. Уже зафиксированные (is_final=True) туры пропускает — "
        "донакручивать там больше нечем (см. round_squad/services.py::"
        "_round_is_complete). Если тур закрывается ИМЕННО этим пересчётом, "
        "автоматически ставится в очередь рассылка «Игрок/сборная тура» "
        "всем верифицированным пользователям."
    ),
}

# Модуль, откуда импортировать функцию задачи — раньше был жёстко захардкожен
# как parsers.tasks для ВСЕХ задач в TRIGGERABLE_TASKS (единственный источник
# на момент задачи #93). С добавлением задач из notifications.tasks выше
# понадобилась явная маршрутизация по имени; TRIGGERABLE_TASKS сознательно
# НЕ тронут по форме (по-прежнему name -> label), чтобы не менять шаблон
# templates/dashboard/parser_tools.html, который просто выводит label.
_TASK_MODULES = {
    "notify_prediction_closing_soon": "notifications.tasks",
    "notify_prediction_results": "notifications.tasks",
    "send_weekly_summary": "notifications.tasks",
    "recompute_all_active_best_xi": "season_squad.tasks",
    "recompute_active_rounds": "round_squad.tasks",
}
_DEFAULT_TASK_MODULE = "parsers.tasks"


def trigger_task(task_name: str) -> tuple[bool, str]:
    if task_name not in TRIGGERABLE_TASKS:
        return False, f"Неизвестная задача: {task_name}"

    debounce_key = f"dashboard:task_debounce:{task_name}"
    if not cache.add(debounce_key, "1", timeout=TASK_DEBOUNCE_SECONDS):
        return False, f"Задача уже запускалась < {TASK_DEBOUNCE_SECONDS}с назад — подождите"

    import importlib

    module = importlib.import_module(_TASK_MODULES.get(task_name, _DEFAULT_TASK_MODULE))
    task_fn = getattr(module, task_name)
    task_fn.delay()
    return True, f"Задача «{TRIGGERABLE_TASKS[task_name]}» поставлена в очередь"


# ============================================================
# Поиск матча — найти UUID/external_id по названию команд
# ============================================================

def available_search_years() -> list[int]:
    """Годы, за которые в базе вообще есть матчи — для выпадающего списка
    в форме поиска (самый свежий год первым)."""
    from matches.models import Match

    years = Match.objects.dates("start_time", "year", order="DESC")
    return [d.year for d in years]


def search_matches(query: str, year: int | None = None) -> dict:
    """
    Поиск матча по названию команд для staff. Чистое число трактуется как
    external_id (KFF game id) — точное совпадение без ограничения по году.

    Иначе — фильтр по календарному году start_time (по умолчанию текущий,
    совпадает с Season.year) вместо произвольного числового лимита: год —
    естественная граница, двухкруговой турнир не даёт сотен матчей одной
    команды за год, а лимит вида [:30] тихо резал старые матчи без намёка,
    что список обрезан. Каждое слово запроса — отдельное AND-условие
    (совпадает с домашней или гостевой), матчинг идёт по normalize_kz()
    в Python (команд — десятки, дешевле, чем SQL TRANSLATE()/unaccent).
    """
    from django.db.models import Q
    from django.utils import timezone

    from matches.models import Match
    from teams.models import Team

    query = (query or "").strip()
    if not query:
        return {"results": [], "total_count": 0, "year": year or timezone.now().year}

    qs = Match.objects.select_related("home_team", "away_team").order_by("-start_time")

    if query.isdigit():
        qs = qs.filter(external_id=query)
    else:
        year = year or timezone.now().year
        qs = qs.filter(start_time__year=year)

        all_teams = list(Team.objects.only("id", "name"))
        # Каждое слово запроса — отдельное AND-условие (совпадает ИЛИ с
        # домашней, ИЛИ с гостевой командой). Одно слово — старое
        # поведение "любая из команд"; несколько слов — сужение до
        # конкретной пары команд.
        for token in query.split():
            normalized_token = normalize_kz(token)
            matching_ids = [t.id for t in all_teams if normalized_token in normalize_kz(t.name)]
            if not matching_ids:
                # Ни одна команда не подходит под этот токен — результатов
                # точно не будет, дальше можно не фильтровать.
                return {"results": [], "total_count": 0, "year": year}
            qs = qs.filter(Q(home_team_id__in=matching_ids) | Q(away_team_id__in=matching_ids))

    results = list(qs)
    return {"results": results, "total_count": len(results), "year": year or timezone.now().year}


# ============================================================
# Живая проверка доступности KFF API (не через очередь celery)
# ============================================================

def kff_api_health_check() -> dict:
    """Синхронный вариант parsers.tasks.health_check_kff_api — staff жмёт
    кнопку и сразу видит результат, а не ставит задачу в очередь и потом
    гадает, выполнилась ли она (celery-версия таски результат никуда не
    показывает, только логирует). Замеряем latency отдельно — "работает,
    но 8 секунд на один запрос" тоже диагностически ценный ответ."""
    client = KFFClient()
    started = time.monotonic()
    try:
        response = client._get("/seasons", params={"tournament": client.TARGET_TOURNAMENT}, retries=1)
        elapsed_ms = round((time.monotonic() - started) * 1000)
        if response:
            return {"ok": True, "status": "Доступен", "elapsed_ms": elapsed_ms}
        return {"ok": False, "status": "Пустой ответ от API", "elapsed_ms": elapsed_ms}
    except Exception as e:
        elapsed_ms = round((time.monotonic() - started) * 1000)
        logger.error(f"kff_api_health_check: {e}", exc_info=True)
        return {"ok": False, "status": f"{type(e).__name__}: {e}", "elapsed_ms": elapsed_ms}


# ============================================================
# Инспекция очереди celery — что выполняется/ждёт ПРЯМО СЕЙЧАС
# ============================================================

def list_active_celery_tasks() -> dict:
    """active() — уже выполняются воркером, reserved() — забраны воркером,
    но ещё не стартовали (например, ждут rate_limit, см. update_match_statuses
    с rate_limit="30/m"). Разделяем эти два состояния в UI, потому что
    диагностика разная: если задача "активна" 20 минут — она зависла и
    кандидат на revoke; если она "зарезервирована" — это нормально, просто
    ждёт своей очереди по rate_limit."""
    from dopx.celery import app

    try:
        inspector = app.control.inspect(timeout=1.5)
        active = inspector.active() or {}
        reserved = inspector.reserved() or {}
    except Exception as e:
        logger.warning(f"list_active_celery_tasks: {e}")
        return {"ok": False, "error": str(e), "active": [], "reserved": []}

    def _flatten(by_worker: dict, state: str) -> list:
        rows = []
        for worker_name, tasks in by_worker.items():
            for t in tasks:
                rows.append({
                    "worker": worker_name,
                    "task_id": t.get("id"),
                    "name": (t.get("name") or "").rsplit(".", 1)[-1],
                    "args": t.get("args"),
                    "state": state,
                })
        return rows

    return {"ok": True, "active": _flatten(active, "active"), "reserved": _flatten(reserved, "reserved")}


def revoke_celery_task(task_id: str, terminate: bool = False) -> tuple[bool, str]:
    """Снять задачу с выполнения/из очереди. `terminate=False` по умолчанию
    — мягкая отмена (задача, которая ещё не началась, просто не запустится;
    уже выполняющаяся — доработает текущий шаг). SIGKILL воркера посреди
    записи в БД может оставить транзакцию в неопределённом состоянии,
    поэтому terminate=True не выставляем по умолчанию из UI — только явный
    флаг, если staff осознанно решит, что задача зависла безнадёжно."""
    from dopx.celery import app

    if not task_id:
        return False, "Не указан task_id"

    try:
        app.control.revoke(task_id, terminate=terminate)
    except Exception as e:
        logger.error(f"revoke_celery_task({task_id}): {e}", exc_info=True)
        return False, f"{type(e).__name__}: {e}"

    return True, f"Задача {task_id} {'принудительно остановлена' if terminate else 'отозвана'}"
