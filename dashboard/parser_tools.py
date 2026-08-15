# dashboard/parser_tools.py
"""
Инструменты ручной работы с KFF-парсером для staff (продуктовый апгрейд,
"парсер/тест-тулинг" — раньше всё это делалось через `python manage.py
shell` или `sync_kff --match-id ... --debug-api`, теперь доступно из
браузера). Три возможности:
  1. raw_kff_response()   — сырой JSON от конкретного эндпоинта KFF API.
  2. resync_match()       — досинхронизировать ОДИН матч (import_full_match).
  3. TRIGGERABLE_TASKS    — реестр celery-задач, которые можно запустить
     вручную с дебаунсом (не дать staff случайно наспамить одну и ту же
     задачу в очередь десять раз подряд кликами).
"""
from __future__ import annotations

import json
import logging

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

TRIGGERABLE_TASKS = {
    "update_match_statuses": "Обновить статусы/события live- и scheduled-матчей",
    "sync_kff_premier_league": "Полный синк текущего сезона Премьер-Лиги",
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
