# dashboard/parser_tools.py
"""
Инструменты ручной работы с KFF-парсером для staff (продуктовый апгрейд,
"парсер/тест-тулинг" — раньше всё это делалось через `python manage.py
shell` или `sync_kff --match-id ... --debug-api`, теперь доступно из
браузера). Возможности:
  1. raw_kff_response()   — сырой JSON от конкретного эндпоинта KFF API.
  2. resync_match()       — досинхронизировать ОДИН матч (import_full_match).
  3. TRIGGERABLE_TASKS    — реестр celery-задач, которые можно запустить
     вручную с дебаунсом (не дать staff случайно наспамить одну и ту же
     задачу в очередь десять раз подряд кликами).
  4. search_matches()     — найти UUID/external_id матча по названию команд
     (раньше единственный способ найти "тот самый" матч — листать сотни
     строк в Django admin или знать UUID заранее).
  5. kff_api_health_check() — синхронный пинг внешнего KFF API с замером
     latency, для быстрого ответа на вопрос "это мы сломались или у них
     API лежит" без необходимости лезть в логи celery.
  6. list_active_celery_tasks() / revoke_celery_task() — что реально
     выполняется/ждёт в очереди ПРЯМО СЕЙЧАС и возможность прибить
     зависшую задачу, не перезапуская весь воркер.
"""
from __future__ import annotations

import json
import logging
import time

from django.core.cache import cache

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

# ИСПРАВЛЕНО: раньше здесь были зарегистрированы только 2 из 7 реально
# существующих задач в parsers/tasks.py (update_match_statuses,
# sync_kff_premier_league) — остальные 5 можно было запустить ТОЛЬКО через
# `python manage.py shell` на сервере, что бесполезно для staff без доступа
# к терминалу. Добавлены остальные безопасные к ручному запуску задачи.
# sync_full_season НЕ включена намеренно — потенциально очень долгая и
# тяжёлая операция (весь сезон целиком), риск случайного клика слишком
# высок; для полного пересинка сезона предусмотрен осознанный путь через
# management-команду с сервера, а не кнопка в UI.
TRIGGERABLE_TASKS = {
    "update_match_statuses": "Обновить статусы/события live- и scheduled-матчей",
    "sync_kff_premier_league": "Полный синк текущего сезона Премьер-Лиги",
    "sync_recent_matches": "Досинхронизировать последние завершённые матчи",
    "check_sync_errors_and_alert": "Проверить ошибки синка за 24ч и отправить алерт при превышении порога",
    "sync_all_enabled_tournaments": "Синхронизировать все включённые турниры (PARSER_SETTINGS)",
}


def trigger_task(task_name: str) -> tuple[bool, str]:
    if task_name not in TRIGGERABLE_TASKS:
        return False, f"Неизвестная задача: {task_name}"

    debounce_key = f"dashboard:task_debounce:{task_name}"
    if not cache.add(debounce_key, "1", timeout=TASK_DEBOUNCE_SECONDS):
        return False, f"Задача уже запускалась < {TASK_DEBOUNCE_SECONDS}с назад — подождите"

    from parsers import tasks as parser_tasks

    task_fn = getattr(parser_tasks, task_name)
    task_fn.delay()
    return True, f"Задача «{TRIGGERABLE_TASKS[task_name]}» поставлена в очередь"


# ============================================================
# Поиск матча — найти UUID/external_id по названию команд
# ============================================================

def search_matches(query: str, limit: int = 20) -> list:
    """Поиск по названию домашней/гостевой команды (icontains) — самый
    частый сценарий staff: "видел проблему у матча КАЙРАТ вчера, какой у
    него UUID?". Отдельно: если запрос — чистое число, считаем это
    external_id (KFF game id) и ищем ТОЧНОЕ совпадение, потому что
    external_id как раз то, что staff копирует из сырого JSON KFF API,
    но нигде в UI не может обратно превратить в наш UUID/ссылку на матч."""
    from matches.models import Match

    query = (query or "").strip()
    if not query:
        return []

    qs = Match.objects.select_related("home_team", "away_team").order_by("-start_time")

    if query.isdigit():
        qs = qs.filter(external_id=query)
    else:
        from django.db.models import Q

        qs = qs.filter(Q(home_team__name__icontains=query) | Q(away_team__name__icontains=query))

    return list(qs[:limit])


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
