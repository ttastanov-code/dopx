# dashboard/parser_tools.py
"""Ручное управление парсером Sportmonks из staff-панели:
запуск задач, health-check API, ресинк матча, инспекция и отмена celery-задач.
"""
from __future__ import annotations

import logging
import time

from django.core.cache import cache

from core.utils import normalize_kz
from parsers.sportmonks.client import SportmonksAPIError, SportmonksClient, get_request_counts

logger = logging.getLogger(__name__)


def resync_match(match) -> tuple[bool, str]:
    """Полный ресинк одного матча (из админки и из «Здоровья данных»).
    Лигу/сезон берём с самого матча — он может быть архивным.
    """
    from parsers.sportmonks import importers

    if not match.sportmonks_id:
        return False, (
            "У матча нет sportmonks_id — синхронизация невозможна "
            "(старый матч из истории KFF)"
        )

    try:
        client = SportmonksClient()
        full = client.get_fixture(int(match.sportmonks_id), include=importers.HEAVY_FIXTURE_INCLUDE)
        importers.import_full_fixture(full, league=match.league, season=match.season)
    except SportmonksAPIError as e:
        logger.error(f"resync_match({match.id}): {e}", exc_info=True)
        return False, f"{type(e).__name__}: {e}"
    except Exception as e:
        logger.error(f"resync_match({match.id}): {e}", exc_info=True)
        return False, f"{type(e).__name__}: {e}"

    return True, f"Матч {match} пересинхронизирован"


# ============================================================
# Ручной запуск celery-задач с дебаунсом
# ============================================================

TASK_DEBOUNCE_SECONDS = 60

# Только лёгкие задачи. Полный бэкафилл — через команду sync_sportmonks_season.
SPORTMONKS_TRIGGERABLE_TASKS = {
    "sportmonks_update_live": "Обновить live-матчи",
    "sportmonks_update_upcoming": "Подтянуть ближайшие составы",
    "sportmonks_sync_season": "Сверить календарь сезона",
    "sportmonks_sync_sidelined": "Обновить травмы/дисквалификации",
    "sportmonks_sync_coach_activity": "Обновить статус тренеров",
    "sportmonks_resync_recent_stats": "Досинкать статистику недавних матчей",
}

# Короткие описания под кнопками.
SPORTMONKS_TASK_DESCRIPTIONS: dict[str, str] = {
    "sportmonks_update_live": "Один запрос по всем live-матчам сразу, при расхождении — полная догрузка. Тикает само каждые 1-2 мин.",
    "sportmonks_update_upcoming": "Подтягивает составы, как только Sportmonks их публикует — обычно за 10-15 мин до старта.",
    "sportmonks_sync_season": "Сверяет календарь сезона с базой, догружает только расхождения. Не трогает уже согласованные матчи.",
    "sportmonks_sync_sidelined": "Травмы и дисквалификации по всем командам лиги. Безопасно в любой момент.",
    "sportmonks_sync_coach_activity": "Кто из тренеров реально ещё активен — без обращения к API, по уже импортированным матчам.",
    "sportmonks_resync_recent_stats": "Досчитывает статистику матчей, завершившихся за последние 3 часа — Sportmonks иногда уточняет цифры уже после финального свистка.",
}

# Задачи, не привязанные к парсеру.
TRIGGERABLE_TASKS = {
    "check_sync_errors_and_alert": "Проверить ошибки синка за 24ч",
    # Пересчёт турнирной таблицы сейчас (обычно — раз в 10 минут для активного сезона).
    "recalculate_season_standings": "Пересчитать турнирную таблицу",
    # Retention-задачи — для ручной проверки без ожидания крона.
    "notify_prediction_closing_soon": "Прогнозы: приглашение за час до старта",
    "notify_prediction_results": "Прогнозы: результат vs прогноз",
    "send_weekly_summary": "Недельная сводка активности",
    "notify_ratings_published": "Push: рейтинги матча открыты",
    # Пересчёт сборной сезона.
    "recompute_all_active_best_xi": "Сборная DOPX: пересчитать сейчас",
    # Пересчёт сборных открытых туров.
    "recompute_active_rounds": "DOPX Лучшие тура: пересчитать сейчас",
    # Пересчёт сборных закрытых туров (is_final=True).
    "recompute_all_closed_rounds_task": "DOPX Лучшие тура: пересчитать закрытые туры",
}

# Описания под кнопками. Ключи совпадают с TRIGGERABLE_TASKS.
TASK_DESCRIPTIONS: dict[str, str] = {
    "check_sync_errors_and_alert": "Смотрит на матчи за 24ч, шлёт алерт админу при проблемах. Только диагностика, ничего не меняет.",
    "recalculate_season_standings": "Пересчитывает турнирную таблицу по активным сезонам. Тикает само каждые 10 мин — жать нужно только сразу после бэкафилла.",
    "notify_prediction_closing_soon": "Приглашение сделать прогноз тем, у кого матч через час, а прогноза ещё нет.",
    "notify_prediction_results": "«Ваш прогноз vs результат» по завершённым матчам.",
    "send_weekly_summary": "Персональная сводка активности за неделю всем активным пользователям.",
    "notify_ratings_published": "Оценившим и болельщикам — итоги по матчам, где голосование закрылось за последние 6 часов. Повторно не шлёт.",
    "recompute_all_active_best_xi": "Пересчитывает «Сборную DOPX» по активным сезонам сейчас, не дожидаясь тика раз в 15 мин.",
    "recompute_active_rounds": "Пересчитывает туры с завершённым матчем, но ещё не зафиксированным составом. Закрытые туры пропускает.",
    "recompute_all_closed_rounds_task": "Пересчитывает состав ВСЕХ уже закрытых туров — для случаев, когда данные матча поправили постфактум. Письма повторно не шлёт.",
}

# Модуль задачи; по умолчанию parsers.tasks.
_TASK_MODULES = {
    "notify_prediction_closing_soon": "notifications.tasks",
    "notify_prediction_results": "notifications.tasks",
    "send_weekly_summary": "notifications.tasks",
    "notify_ratings_published": "notifications.tasks",
    "recompute_all_active_best_xi": "season_squad.tasks",
    "recompute_active_rounds": "round_squad.tasks",
    "recompute_all_closed_rounds_task": "round_squad.tasks",
    "recalculate_season_standings": "aggregates.tasks",
    "sportmonks_update_live": "parsers.sportmonks.tasks",
    "sportmonks_update_upcoming": "parsers.sportmonks.tasks",
    "sportmonks_sync_season": "parsers.sportmonks.tasks",
    "sportmonks_sync_sidelined": "parsers.sportmonks.tasks",
    "sportmonks_sync_coach_activity": "parsers.sportmonks.tasks",
    "sportmonks_resync_recent_stats": "parsers.sportmonks.tasks",
}
_DEFAULT_TASK_MODULE = "parsers.tasks"


def trigger_task(task_name: str) -> tuple[bool, str]:
    # Общий lookup по обоим словарям.
    all_tasks = {**TRIGGERABLE_TASKS, **SPORTMONKS_TRIGGERABLE_TASKS}
    if task_name not in all_tasks:
        return False, f"Неизвестная задача: {task_name}"

    debounce_key = f"dashboard:task_debounce:{task_name}"
    if not cache.add(debounce_key, "1", timeout=TASK_DEBOUNCE_SECONDS):
        return False, f"Задача уже запускалась < {TASK_DEBOUNCE_SECONDS}с назад — подождите"

    import importlib

    module = importlib.import_module(_TASK_MODULES.get(task_name, _DEFAULT_TASK_MODULE))
    task_fn = getattr(module, task_name)
    task_fn.delay()
    return True, f"Задача «{all_tasks[task_name]}» поставлена в очередь"


# ============================================================
# Поиск матча по названию команд
# ============================================================

def available_search_years() -> list[int]:
    """Годы, за которые есть матчи (свежие первыми)."""
    from matches.models import Match

    years = Match.objects.dates("start_time", "year", order="DESC")
    return [d.year for d in years]


def search_matches(query: str, year: int | None = None) -> dict:
    """Поиск матча для staff. Число — sportmonks_id.
    Иначе фильтр по году и словам запроса (каждое слово — AND, по normalize_kz).
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
        qs = qs.filter(sportmonks_id=query)
    else:
        year = year or timezone.now().year
        qs = qs.filter(start_time__year=year)

        all_teams = list(Team.objects.only("id", "name"))
        # Каждое слово — отдельное AND-условие (хозяева ИЛИ гости).
        for token in query.split():
            normalized_token = normalize_kz(token)
            matching_ids = [t.id for t in all_teams if normalized_token in normalize_kz(t.name)]
            if not matching_ids:
                # Ни одна команда не подошла — дальше не ищем.
                return {"results": [], "total_count": 0, "year": year}
            qs = qs.filter(Q(home_team_id__in=matching_ids) | Q(away_team_id__in=matching_ids))

    results = list(qs)
    return {"results": results, "total_count": len(results), "year": year or timezone.now().year}


# ============================================================
# Проверка доступности Sportmonks API (синхронно)
# ============================================================

# Последняя проверка хранится без TTL. Лимит — 2000 запросов/час на entity.
SPORTMONKS_HOURLY_LIMIT = 2000

SPORTMONKS_HEALTH_CACHE_KEY = "dashboard:sportmonks_health_last"


def get_cached_sportmonks_health() -> dict | None:
    """Последний результат проверки или None."""
    return cache.get(SPORTMONKS_HEALTH_CACHE_KEY)


def sportmonks_api_health_check() -> dict:
    """Синхронная проверка API через get_league(): статус, latency, расход лимита.
    Результат кэшируется.
    """
    from django.utils import timezone as _timezone

    client = SportmonksClient()
    started = time.monotonic()
    try:
        league_data = client.get_league()
        elapsed_ms = round((time.monotonic() - started) * 1000)
        if league_data:
            result = {
                "ok": True, "status": f"Доступен ({league_data.get('name', '—')})",
                "elapsed_ms": elapsed_ms,
            }
        else:
            result = {"ok": False, "status": "Пустой ответ от API", "elapsed_ms": elapsed_ms}
    except SportmonksAPIError as e:
        elapsed_ms = round((time.monotonic() - started) * 1000)
        logger.error(f"sportmonks_api_health_check: {e}", exc_info=True)
        result = {"ok": False, "status": f"{type(e).__name__}: {e}", "elapsed_ms": elapsed_ms}
    except Exception as e:
        elapsed_ms = round((time.monotonic() - started) * 1000)
        logger.error(f"sportmonks_api_health_check: {e}", exc_info=True)
        result = {"ok": False, "status": f"{type(e).__name__}: {e}", "elapsed_ms": elapsed_ms}

    # Остаток лимита из последнего ответа.
    rate_limit = client.last_rate_limit or {}
    remaining = rate_limit.get("remaining")
    result["rate_limit_remaining"] = remaining
    result["rate_limit_entity"] = rate_limit.get("requested_entity")
    # Израсходовано = лимит - остаток.
    result["rate_limit_used"] = (
        SPORTMONKS_HOURLY_LIMIT - remaining if remaining is not None else None
    )
    result["rate_limit_total"] = SPORTMONKS_HOURLY_LIMIT

    # Наш счётчик запросов за час/сутки (по всем задачам).
    result["request_counts"] = get_request_counts()

    result["checked_at"] = _timezone.now()
    cache.set(SPORTMONKS_HEALTH_CACHE_KEY, result, timeout=None)
    return result


# ============================================================
# Очередь celery — что выполняется/ждёт сейчас
# ============================================================

def list_active_celery_tasks() -> dict:
    """active() — уже выполняются, reserved() — взяты воркером, но ещё не стартовали."""
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
    """Отменить задачу. По умолчанию мягко (terminate=False)."""
    from dopx.celery import app

    if not task_id:
        return False, "Не указан task_id"

    try:
        app.control.revoke(task_id, terminate=terminate)
    except Exception as e:
        logger.error(f"revoke_celery_task({task_id}): {e}", exc_info=True)
        return False, f"{type(e).__name__}: {e}"

    return True, f"Задача {task_id} {'принудительно остановлена' if terminate else 'отозвана'}"
