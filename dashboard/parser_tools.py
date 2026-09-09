# dashboard/parser_tools.py
"""
Ручное управление парсером из staff-панели. Sportmonks — ЕДИНСТВЕННЫЙ
источник данных матчей (2026-09-09: KFF-парсер, его клиент, импортёры и весь
rollback-путь физически удалены по решению пользователя — см. историю чата
и ADR-0044/0045, которые описывают уже устаревшее двух-источниковое
состояние проекта).

  - SPORTMONKS_TRIGGERABLE_TASKS / sportmonks_api_health_check() — ручной
    запуск лёгких/безопасных задач синка и live-пинг API. Сырого JSON-вьюера
    нет: у Sportmonks-клиента разные сигнатуры на метод (fixture_id,
    диапазон дат, include-строка) — общий выпадающий список по одному
    external_id тут не подходит (в отличие от того, что было у KFF).
  - resync_match() — точечный ресинк ОДНОГО матча по его sportmonks_id.
  - TRIGGERABLE_TASKS — источник-агностичные задачи (диагностика,
    нотификации, пересчёт составов), не связанные с конкретным парсером.
  - Инспекция/отмена celery-задач — общая для всей очереди.
"""
from __future__ import annotations

import logging
import time

from django.core.cache import cache

from core.utils import normalize_kz
from parsers.sportmonks.client import SportmonksAPIError, SportmonksClient, get_request_counts

logger = logging.getLogger(__name__)


def resync_match(match) -> tuple[bool, str]:
    """Полный ресинк ОДНОГО матча — вызывается и из admin (matches/admin.py::
    resync_selected), и из /staff/dashboard/data-health/ (кнопка
    «Досинхронизировать» у каждого проблемного матча). Тот же тяжёлый вызов
    (полный include), что и _heavy_sync_fixture в parsers/sportmonks/tasks.py,
    но league/season берём напрямую с самого матча (Match.league/
    Match.season), а не через _get_league_and_season() — та функция
    резолвит ТЕКУЩИЙ активный сезон, здесь же нужен сезон КОНКРЕТНОГО матча
    (может быть архивным)."""
    from parsers.sportmonks import importers

    if not match.sportmonks_id:
        return False, (
            "У матча нет sportmonks_id — синхронизация невозможна "
            "(матч, видимо, из истории KFF-эпохи, до 2026-09-09; "
            "автоматической пересинхронизации для таких матчей больше нет)"
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

# Все 4 задачи ниже — намеренно только "лёгкие"/инкрементальные: полный
# бэкафилл сезона с нуля — отдельная management-команда
# (sync_sportmonks_season), а не кнопка в UI — риск случайного клика на
# тяжёлую операцию слишком высок.
SPORTMONKS_TRIGGERABLE_TASKS = {
    "sportmonks_update_live": "Обновить live-матчи (лёгкий bulk-опрос + тяжёлая догрузка изменившихся)",
    "sportmonks_update_upcoming": "Подтянуть составы для матчей в ближайшие 3 часа",
    "sportmonks_sync_season": "Сверить календарь активного сезона (новые/изменившиеся матчи)",
    "sportmonks_sync_sidelined": "Обновить травмы/дисквалификации по всем командам лиги",
    "sportmonks_sync_coach_activity": "Обновить статус тренеров (кто реально ещё активен)",
}

SPORTMONKS_TASK_DESCRIPTIONS: dict[str, str] = {
    "sportmonks_update_live": (
        "Один bulk-запрос ко всем live-фикстурам лиги сразу (не по одному "
        "матчу — экономия лимита, см. докстринг parsers/sportmonks/tasks.py). "
        "Если статус/счёт разошёлся с базой — тяжёлая догрузка полного "
        "состава/событий/статистики ТОЛЬКО для изменившегося матча. "
        "Работает и в расписании каждые 1-2 минуты — жать вручную нужно "
        "только для немедленной проверки прямо сейчас."
    ),
    "sportmonks_update_upcoming": (
        "Ищет матчи в ближайшие 3 часа без состава (has_lineup=False) и "
        "тяжело догружает каждый — расстановки на Sportmonks появляются "
        "за 10-15 минут до старта, эта задача подхватывает их раньше, чем "
        "следующий плановый тик."
    ),
    "sportmonks_sync_season": (
        "Лёгкая сверка: запрашивает календарь всего активного сезона "
        "(без тяжёлых include), сравнивает статус/счёт с базой и тяжело "
        "догружает ТОЛЬКО расхождения (новый матч, перенос даты, "
        "изменившийся счёт). НЕ трогает повторно уже согласованные "
        "завершённые матчи — не полный бэкафилл, для него есть отдельная "
        "management-команда на сервере."
    ),
    "sportmonks_sync_sidelined": (
        "По одному лёгкому запросу на каждую команду лиги — травмы и "
        "дисквалификации под бейдж «недоступен» на карточке игрока "
        "(players/models.py::PlayerSidelined). Безопасно жать в любой момент."
    ),
    "sportmonks_sync_coach_activity": (
        "БЕЗ обращения к API — считает по уже импортированным матчам, кто "
        "реально сейчас тренер команды (последний сыгранный матч), и "
        "снимает «Активен» с тех, кто перестал появляться в новых матчах "
        "(coaches/services.py::refresh_coach_activity). Нажмите один раз "
        "сразу после большого бэкафилла — иначе ждать до 04:30 по расписанию."
    ),
}

# Источник-агностичные задачи: не привязаны к конкретному парсеру, поэтому
# живут отдельно от SPORTMONKS_TRIGGERABLE_TASKS выше.
TRIGGERABLE_TASKS = {
    "check_sync_errors_and_alert": "Проверить ошибки синка за 24ч и отправить алерт при превышении порога",
    # 2026-09-09 (продуктовый фидбек: "не хватает кнопки пересчёта агрегатов,
    # мы синк сделали, а таблица не пересчитанная") — TeamSeasonStats
    # (турнирная таблица) пересчитывается по расписанию раз в 10 минут
    # ТОЛЬКО для активного сезона (aggregates/tasks.py::
    # recalculate_season_standings), после ручного бэкафилла нескольких
    # сезонов сразу ждать тик неудобно — кнопка форсирует прямо сейчас.
    "recalculate_season_standings": "Пересчитать турнирную таблицу сейчас (активные сезоны)",
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
# СТРОГО совпадают с TRIGGERABLE_TASKS.
TASK_DESCRIPTIONS: dict[str, str] = {
    "check_sync_errors_and_alert": (
        "Смотрит на матчи/события за последние 24 часа — если много "
        "завершённых матчей без составов/событий, шлёт email-алерт админу. "
        "Ничего не синхронизирует и не меняет данные, только диагностика."
    ),
    "recalculate_season_standings": (
        "Пересчитывает П/В/Н/П, разницу мячей и место в таблице (TeamSeasonStats) "
        "по уже импортированным матчам — для ВСЕХ сезонов с is_active=True сразу. "
        "Обычно тикает каждые 10 минут само; жать вручную нужно только сразу "
        "после ручного бэкафилла/ресинка, чтобы не ждать. Для ЗАВЕРШЁННЫХ (не "
        "активных) сезонов эта кнопка не поможет — там таблица считается один "
        "раз лениво при первом заходе на страницу лиги/команды."
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

# Модуль, откуда импортировать функцию задачи — по умолчанию parsers.tasks
# (check_sync_errors_and_alert), явная маршрутизация для задач из других
# приложений.
_TASK_MODULES = {
    "notify_prediction_closing_soon": "notifications.tasks",
    "notify_prediction_results": "notifications.tasks",
    "send_weekly_summary": "notifications.tasks",
    "recompute_all_active_best_xi": "season_squad.tasks",
    "recompute_active_rounds": "round_squad.tasks",
    "recalculate_season_standings": "aggregates.tasks",
    "sportmonks_update_live": "parsers.sportmonks.tasks",
    "sportmonks_update_upcoming": "parsers.sportmonks.tasks",
    "sportmonks_sync_season": "parsers.sportmonks.tasks",
    "sportmonks_sync_sidelined": "parsers.sportmonks.tasks",
    "sportmonks_sync_coach_activity": "parsers.sportmonks.tasks",
}
_DEFAULT_TASK_MODULE = "parsers.tasks"


def trigger_task(task_name: str) -> tuple[bool, str]:
    # Единый lookup по объединению обоих словарей (Sportmonks + общие) —
    # держим одну функцию/один URL для обеих групп, а не дублируем
    # view+debounce+audit-логирование под каждую с нуля.
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
# Поиск матча — найти UUID по названию команд
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
    sportmonks_id — точное совпадение без ограничения по году.

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
        qs = qs.filter(sportmonks_id=query)
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
# Живая проверка доступности Sportmonks API (не через очередь celery)
# ============================================================

# Кэшируем результат последней проверки (без TTL — переживает рестарт
# воркера/дев-сервера ничем не хуже, чем "неизвестно совсем", устаревшее
# значение с явным "проверено N назад" полезнее пустой карточки при первом
# заходе на страницу). Ключ намеренно НЕ завязан на request/пользователя —
# это глобальный факт про API, а не персональное состояние staff.
# Подтверждено вживую тестовым ключом на реальных данных КПЛ (см. докстринг
# модуля parsers/sportmonks/client.py) — 2000 запросов в час НА КАЖДУЮ entity
# отдельно. Sportmonks не отдаёт сам лимит в ответе (только remaining), так
# что "израсходовано" считаем сами как total - remaining.
SPORTMONKS_HOURLY_LIMIT = 2000

SPORTMONKS_HEALTH_CACHE_KEY = "dashboard:sportmonks_health_last"


def get_cached_sportmonks_health() -> dict | None:
    """Последний результат sportmonks_api_health_check() (сам вызов или
    ручной клик «Проверить») — для отрисовки карточки на /staff/dashboard/
    parser-tools/ БЕЗ похода в сеть при каждой загрузке страницы. None,
    если проверка не запускалась вообще ни разу с последнего сброса кэша."""
    return cache.get(SPORTMONKS_HEALTH_CACHE_KEY)


def sportmonks_api_health_check() -> dict:
    """Синхронный вызов — staff жмёт кнопку и сразу видит результат, а не
    ставит задачу в очередь и потом гадает, выполнилась ли она (celery-
    версия — sportmonks_health_check в parsers/sportmonks/tasks.py — только
    логирует результат). Замеряем latency отдельно — "работает, но 8 секунд
    на один запрос" тоже диагностически ценный ответ. Дергаем get_league() —
    самый дешёвый содержательный вызов (не livescores/fixtures, которые
    могут легитимно вернуть пустой список и тогда latency-замер ничего не
    скажет о самом факте доступности API).

    Результат кэшируется (см. SPORTMONKS_HEALTH_CACHE_KEY выше) с меткой
    времени — чтобы карточка на странице показывала последний известный
    статус сразу при загрузке, а не пустое место до первого клика."""
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

    # Расход лимита (2026-09-09, продуктовый фидбек: "было бы неплохо
    # подключить текущее кол-во запросов") — Sportmonks кладёт остаток в
    # КАЖДЫЙ ответ (client.py::_log_rate_limit_if_low), клиент сохраняет
    # последний себе в last_rate_limit. None, если поле отсутствует в
    # ответе (например при ошибке до получения payload). ИСПРАВЛЕНО
    # (2026-09-09, баг найден пользователем — last_rate_limit никогда
    # фактически не присваивался, см. client.py::_log_rate_limit_if_low).
    rate_limit = client.last_rate_limit or {}
    remaining = rate_limit.get("remaining")
    result["rate_limit_remaining"] = remaining
    result["rate_limit_entity"] = rate_limit.get("requested_entity")
    # "Кол-во уже израсходованных запросов" — прямо запрошено пользователем,
    # remaining-only было недостаточно наглядно. SPORTMONKS_HOURLY_LIMIT —
    # подтверждено вживую тестовым ключом на реальных данных КПЛ (см.
    # докстринг модуля client.py, "2000 запросов в час на каждую entity
    # отдельно"), Sportmonks не отдаёт сам лимит в ответе, только remaining.
    result["rate_limit_used"] = (
        SPORTMONKS_HOURLY_LIMIT - remaining if remaining is not None else None
    )
    result["rate_limit_total"] = SPORTMONKS_HOURLY_LIMIT

    # ИСПРАВЛЕНО (2026-09-09, баг найден пользователем — "вообще не
    # сходится кол-во запросов израсходованных", сравнил со своим порталом
    # Sportmonks: 871 запрос за день против нашего "1"): rate_limit_used
    # выше — это остаток ТОЛЬКО по entity последнего вызова (здесь всегда
    # "League", т.к. health-check делает один get_league()), а не суммарный
    # расход. request_counts — наш собственный счётчик (client.py::
    # get_request_counts/_track_request), инкрементируется на КАЖДЫЙ
    # реальный HTTP-запрос к Sportmonks из ЛЮБОЙ задачи/команды, не только
    # из этой проверки — тот показатель, который реально сопоставим с
    # порталом Sportmonks (хотя и не побитово идентичен — другие границы
    # часа/суток на их стороне).
    result["request_counts"] = get_request_counts()

    result["checked_at"] = _timezone.now()
    cache.set(SPORTMONKS_HEALTH_CACHE_KEY, result, timeout=None)
    return result


# ============================================================
# Инспекция очереди celery — что выполняется/ждёт ПРЯМО СЕЙЧАС
# ============================================================

def list_active_celery_tasks() -> dict:
    """active() — уже выполняются воркером, reserved() — забраны воркером,
    но ещё не стартовали (например, ждут rate_limit). Разделяем эти два
    состояния в UI, потому что диагностика разная: если задача "активна"
    20 минут — она зависла и кандидат на revoke; если она "зарезервирована"
    — это нормально, просто ждёт своей очереди по rate_limit."""
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
