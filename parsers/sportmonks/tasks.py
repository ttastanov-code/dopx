# parsers/sportmonks/tasks.py
"""
Celery-задачи синхронизации с Sportmonks (фаза 4, docs/sportmonks-migration-
plan.md). Живут ПАРАЛЛЕЛЬНО с parsers/tasks.py (KFF) — ничего там не
трогаем, cutover (переключение CELERY_BEAT_SCHEDULE) — отдельный шаг фазы 6,
после нескольких дней параллельного прогона и ручной сверки.

ПРИНЦИП ЭКОНОМИИ ЛИМИТА (см. план, фаза 4 — "никогда не дёргать матчи в
цикле по одному во время live-опроса"): двухуровневая схема.
  1. ЛЁГКИЙ опрос (sportmonks_update_live, каждые 1-2 минуты) — ОДИН bulk
     вызов client.get_livescores() на всю лигу сразу (см. докстринг
     client.py про 240 запросов/час независимо от числа одновременных
     матчей vs. 1440+ запросов/час при цикле по каждому матчу). Сравнивает
     статус/счёт с тем, что уже в базе.
  2. ТЯЖЁЛАЯ догрузка (_heavy_sync_fixture) — отдельный вызов с полным
     include (HEAVY_FIXTURE_INCLUDE) СТРОГО ПО ОДНОМУ fixture_id, и СТРОГО
     только для матчей, где шаг 1 обнаружил реальное изменение. Даже в день
     с 5-6 одновременными матчами это на порядок дешевле лимита 2000/час
     на entity Fixture — см. расчёт в переписке с пользователем при
     проектировании плана.

Redis-лок на fixture (не на всю задачу) — тот же принцип, что
parsers/tasks.py::_acquire_match_sync_lock (KFF), но своё пространство
ключей ("sportmonks:" вместо "parsers:") — чтобы live/upcoming таски не
дублировали тяжёлую догрузку одного и того же матча, если тики
пересеклись (текущий прогон не успел закончиться до следующего) либо
сработали одновременно live- и upcoming-таски на один матч в переходный
момент (близко к kickoff).
"""
from __future__ import annotations

import logging

from celery import shared_task
from django.core.cache import cache
from django.utils import timezone
from datetime import timedelta

from leagues.models import League
from matches.models import Match
from parsers.models import ParserSyncRun
from parsers.sportmonks import importers
from parsers.sportmonks.client import SportmonksAPIError, SportmonksClient
from seasons.models import Season
from teams.models import Team

logger = logging.getLogger(__name__)

# TTL с запасом относительно самого частого расписания (*/1-2 минуты) —
# тот же принцип, что MATCH_SYNC_LOCK_TIMEOUT_SECONDS в parsers/tasks.py:
# лок должен гарантированно "протухнуть", даже если воркер упал посреди
# тяжёлой догрузки и не выполнил finally: cache.delete(...).
FIXTURE_SYNC_LOCK_TIMEOUT_SECONDS = 120

# Насколько заранее до стартового свистка начинаем тянуть составы —
# подтверждено вживую менеджером Sportmonks: расстановки появляются за
# 10-15 минут до матча. 3 часа с запасом — задача сама по себе лёгкая
# (несколько heavy-fetch на близкие матчи раз в 30 минут), рано подхватить
# дешевле, чем пропустить и ждать следующего тика после того, как состав
# уже появился.
UPCOMING_WINDOW = timedelta(hours=3)


def _record_sync_run(task_name: str, started_at, total: int = 0, updated: int = 0, unchanged: int = 0, errors: int = 0) -> None:
    """Пишет ParserSyncRun(source='sportmonks') — та же таблица, что и у
    KFF (parsers/tasks.py::update_match_statuses), теперь с полем `source`
    (миграция 0003_parsersyncrun_source) специально под cutover: dashboard/
    services.py::data_health_summary читает ParserSyncRun.objects.all() без
    фильтра по источнику, значит просто продолжать писать сюда — самый
    дешёвый способ не сломать "последний синк N минут назад" на дашборде.
    Обёрнуто в try, как и у KFF-аналога — сбой записи метрики не должен
    ронять уже выполненную синхронизацию."""
    try:
        ParserSyncRun.objects.create(
            task_name=task_name, source="sportmonks", started_at=started_at,
            total=total, updated=updated, unchanged=unchanged, errors=errors,
        )
    except Exception:
        logger.error("Sportmonks: не удалось записать ParserSyncRun для %s", task_name, exc_info=True)


def _get_league_and_season():
    """Общий резолвер (лига, активный сезон) для всех тасков ниже —
    None-safe: если сезон ещё не активирован (see sync_sportmonks_season
    --> _resolve_current_season_id) или лига ещё не создана, таск должен
    тихо выйти, а не упасть."""
    league = League.objects.filter(sportmonks_id__isnull=False, is_primary=True).first()
    if league is None:
        return None, None
    season = Season.get_primary_active()
    if season is None or not season.sportmonks_id:
        return league, None
    return league, season


def _fixture_lock_key(sportmonks_fixture_id) -> str:
    return f"sportmonks:fixture_sync_lock:{sportmonks_fixture_id}"


def _heavy_sync_fixture(client: SportmonksClient, league, season, sportmonks_fixture_id) -> bool:
    """Тяжёлая догрузка ОДНОГО матча — вызывается ТОЛЬКО когда лёгкий опрос
    обнаружил реальное изменение (см. докстринг модуля). Возвращает True,
    если синк реально произошёл (для логирования количества в вызывающей
    задаче), False — если лок был занят (кто-то другой уже синкает этот же
    матч прямо сейчас) или запрос упал."""
    lock_key = _fixture_lock_key(sportmonks_fixture_id)
    if not cache.add(lock_key, "1", timeout=FIXTURE_SYNC_LOCK_TIMEOUT_SECONDS):
        logger.info("Sportmonks: fixture %s уже синкается другим воркером, пропуск", sportmonks_fixture_id)
        return False
    try:
        full = client.get_fixture(sportmonks_fixture_id, include=importers.HEAVY_FIXTURE_INCLUDE)
        importers.import_full_fixture(full, league=league, season=season)
        return True
    except SportmonksAPIError as exc:
        logger.error("Sportmonks: тяжёлая догрузка fixture %s не удалась: %s", sportmonks_fixture_id, exc)
        return False
    except Exception:
        logger.error("Sportmonks: ошибка импорта fixture %s", sportmonks_fixture_id, exc_info=True)
        return False
    finally:
        cache.delete(lock_key)


@shared_task(bind=True, max_retries=2)
def sportmonks_update_live(self):
    """ЛЁГКИЙ опрос — каждые 1-2 минуты (см. CELERY_BEAT_SCHEDULE). ОДИН
    bulk-вызов на всю лигу, без тяжёлых include (state;participants;scores
    достаточно для сравнения — 'events' здесь намеренно не запрашиваем,
    полные события всё равно приходят через тяжёлую догрузку ниже).

    Пишет ParserSyncRun(source='sportmonks') в конце КАЖДОГО запуска (даже
    если сейчас нет ни одного live-матча) — та же причина, что у KFF's
    update_match_statuses (parsers/tasks.py): dashboard/services.py::
    data_health_summary судит о "жив ли синк" по свежести последней строки.
    Если писать только при реальных изменениях, в тихие часы без live-матчей
    (большую часть суток) дашборд выглядел бы так, будто синк снова "завис",
    хотя задача исправно тикает каждые 2 минуты и просто не находит работы.

    ИСПРАВЛЕНО (2026-09-09, жалоба пользователя: матч Кайрат-Женис навсегда
    "завис" в статусе live — стал live после ручного ресинка в середине
    2-го тайма, и НИ ОДИН последующий тик за двое суток его не поправил,
    включая ручной клик "Обновить live-матчи" в staff-панели) — КОРНЕВАЯ
    ПРИЧИНА: эта задача раньше проходила ТОЛЬКО по списку фикстур, которые
    Sportmonks СЕЙЧАС считает live (`live_fixtures` ниже). Как только матч
    реально заканчивается, Sportmonks перестаёт отдавать его в
    /livescores/inplay — фикстура просто исчезает из ответа, и цикл ниже
    для неё больше НИКОГДА не выполняется. У нас в БД матч остаётся
    status='live' НАВСЕГДА, пока не сработает суточная sportmonks_sync_season
    (а если и она по какой-то причине не отработала — вообще никогда).
    "Обновить live-матчи" в staff-панели дёргает ровно эту же задачу —
    та же слепая зона, поэтому и ручной клик не помогал.

    Фикс: после разбора live_fixtures ДОПОЛНИТЕЛЬНО берём все матчи, которые
    У НАС в БД сейчас status='live', и для тех, чей sportmonks_id НЕ попал в
    свежий live_fixtures (т.е. "выпал из живого списка" — либо закончился,
    либо это transient-пропуск в ответе API), сразу дёргаем тяжёлую догрузку
    — не ждём суточного сведения. Не расходует лимит зря: таких матчей в
    любой момент времени — считаные единицы (одновременно идущих + только
    что завершившихся с прошлого тика), а не весь календарь сезона."""
    started_at = timezone.now()
    league, season = _get_league_and_season()
    if league is None or season is None:
        logger.debug("Sportmonks: нет активной лиги/сезона — sportmonks_update_live пропущен")
        return

    client = SportmonksClient()
    league_sm_id = int(league.sportmonks_id)
    try:
        live_fixtures = client.get_livescores(include="state;participants;scores", league_id=league_sm_id)
    except SportmonksAPIError as exc:
        logger.error("Sportmonks: get_livescores() не удался: %s", exc)
        _record_sync_run("sportmonks_update_live", started_at, total=0, errors=1)
        return

    live_sm_ids_from_api = {str(fx.get("id")) for fx in live_fixtures if fx.get("id") is not None}

    synced = 0
    errors = 0
    for fx in live_fixtures:
        sm_id = fx.get("id")
        if sm_id is None:
            continue

        # P0 (Codex-ревью 2026-09-09, подтверждено по коду) — второй барьер
        # после фильтра в client.py::get_livescores: /livescores/inplay
        # официально не документирован как фильтруемый по лиге, значит
        # доверять ТОЛЬКО query-параметру нельзя. fixture_league_id — поле,
        # которое Sportmonks кладёт в базовый объект фикстуры без доп.
        # include (league_id, не league.id — тот появляется только при
        # include=league). Если поле почему-то отсутствует в ответе —
        # НЕ пропускаем матч вслепую (регресс хуже, чем теоретический
        # риск), но логируем, чтобы это не осталось незамеченным.
        fixture_league_id = fx.get("league_id")
        if fixture_league_id is not None and int(fixture_league_id) != league_sm_id:
            logger.warning(
                "Sportmonks: sportmonks_update_live — fixture %s принадлежит league_id=%s, "
                "а не нашей лиге (%s), пропущен (см. P0 в код-ревью 2026-09-09)",
                sm_id, fixture_league_id, league_sm_id,
            )
            continue

        state = (fx.get("state") or {}).get("developer_name") or ""
        mapped_status = importers.STATE_MAP.get(state, "live")

        home_score = away_score = None
        for score in fx.get("scores") or []:
            if score.get("description") != "CURRENT":
                continue
            score_obj = score.get("score") or {}
            if score_obj.get("participant") == "home":
                home_score = score_obj.get("goals")
            elif score_obj.get("participant") == "away":
                away_score = score_obj.get("goals")

        existing = Match.objects.filter(sportmonks_id=str(sm_id)).only(
            "id", "status", "home_score", "away_score"
        ).first()

        changed = (
            existing is None
            or existing.status != mapped_status
            or existing.home_score != home_score
            or existing.away_score != away_score
        )
        if not changed:
            continue

        if _heavy_sync_fixture(client, league, season, sm_id):
            synced += 1
        else:
            errors += 1

    # См. докстринг выше — матчи, которые У НАС ещё 'live', но уже выпали
    # из свежего live_fixtures. manual_override исключаем намеренно: если
    # staff вручную заморозил статус (mark_postponed_manually и т.п.), это
    # ЕГО решение, автосинк не должен его перебивать — та же гарантия,
    # что и everywhere else в этом файле/importers.py.
    stuck_live = Match.objects.filter(
        league=league, status="live", sportmonks_id__isnull=False, manual_override=False,
    ).exclude(sportmonks_id__in=live_sm_ids_from_api).only("id", "sportmonks_id")

    reconciled = 0
    for match in stuck_live:
        logger.info(
            "Sportmonks: матч %s (sportmonks_id=%s) числится live, но выпал из /livescores/inplay — "
            "досинхронизирую вне очереди (см. фикс 2026-09-09 про 'вечный live')",
            match.id, match.sportmonks_id,
        )
        if _heavy_sync_fixture(client, league, season, match.sportmonks_id):
            reconciled += 1
        else:
            errors += 1

    if synced or reconciled:
        logger.info(
            "Sportmonks: sportmonks_update_live — синкнуто матчей с изменениями: %d, "
            "досведено 'зависших' live: %d", synced, reconciled,
        )

    _record_sync_run(
        "sportmonks_update_live", started_at,
        total=len(live_fixtures) + reconciled, updated=synced + reconciled, errors=errors,
        unchanged=max(len(live_fixtures) - synced - errors, 0),
    )


@shared_task(bind=True, max_retries=2)
def sportmonks_update_upcoming(self):
    """Раз в 30 минут (см. CELERY_BEAT_SCHEDULE) — подтягивает составы для
    матчей в ближайшие UPCOMING_WINDOW часов, у которых их ещё нет
    (has_lineup=False). Дешёвая задача: обычно 0-6 матчей одновременно
    (максимум тура КПЛ), каждый — отдельный тяжёлый вызов, но не в цикле
    ЖИВОГО опроса (см. докстринг модуля)."""
    league, season = _get_league_and_season()
    if league is None or season is None:
        logger.debug("Sportmonks: нет активной лиги/сезона — sportmonks_update_upcoming пропущен")
        return

    now = timezone.now()
    upcoming = Match.objects.filter(
        season=season,
        sportmonks_id__isnull=False,
        status="scheduled",
        has_lineup=False,
        start_time__gte=now,
        start_time__lte=now + UPCOMING_WINDOW,
    ).only("id", "sportmonks_id")

    if not upcoming:
        return

    client = SportmonksClient()
    synced = 0
    for match in upcoming:
        if _heavy_sync_fixture(client, league, season, match.sportmonks_id):
            synced += 1

    if synced:
        logger.info("Sportmonks: sportmonks_update_upcoming — подтянуты составы для %d матчей", synced)


@shared_task(bind=True, max_retries=1)
def sportmonks_sync_season(self):
    """Раз в сутки (см. CELERY_BEAT_SCHEDULE) — лёгкая сверка календаря
    активного сезона: находит НОВЫЕ матчи (перенос дат/новый матч в
    расписании) и матчи, чей статус/счёт разошёлся с базой, догружает
    только их тяжёлым вызовом. НЕ трогает уже финализированные
    ("finished" в базе И у источника без расхождений) матчи повторно —
    иначе это был бы полный бэкафилл каждый день, а не лёгкая сверка.

    ВАЖНО (2026-09-09, вопрос пользователя "переключатель сезона сам
    обновится на 2027?"): раньше, если Sportmonks переставал считать
    текущий у нас в БД активный сезон "текущим" (сезон реально закончился
    и начался следующий), эта задача просто логировала warning и молча
    выходила — is_active на Season НИКОГДА не переставлялся сюда
    автоматически, только ручным перезапуском management-команды
    sync_sportmonks_season. Значит переключатель сезона застревал бы на
    прошлом годе, пока кто-то не вспомнил и не запустил бэкафилл руками.
    Теперь при каждом суточном тике сверяем, не сменился ли "текущий"
    сезон на стороне Sportmonks (та же логика дат, что и в
    _resolve_current_season_id ниже), и если да — сами создаём/активируем
    новый Season (get_or_create_season(..., is_current=True), см. его
    докстринг и Season.save() — снимает is_active со старого сезона
    автоматически) и продолжаем сверку календаря уже для него, без
    ручного вмешательства."""
    league, season = _get_league_and_season()
    if league is None:
        logger.debug("Sportmonks: нет активной лиги — sportmonks_sync_season пропущен")
        return

    client = SportmonksClient()
    try:
        league_data = client.get_league(include="seasons")
    except SportmonksAPIError as exc:
        logger.error("Sportmonks: sportmonks_sync_season — не удалось получить лигу: %s", exc)
        return

    from parsers.management.commands.sync_sportmonks_season import (
        _chunk_date_range, _resolve_current_season_id,
    )

    all_seasons_data = league_data.get("seasons") or []
    current_season_id = _resolve_current_season_id(all_seasons_data)
    season_data = next(
        (s for s in all_seasons_data if str(s.get("id")) == season.sportmonks_id),
        None,
    ) if season else None

    if current_season_id is not None and (season is None or str(current_season_id) != season.sportmonks_id):
        new_season_data = next((s for s in all_seasons_data if s.get("id") == current_season_id), None)
        if new_season_data is not None:
            logger.info(
                "Sportmonks: активный сезон сменился (был %s) — активирую сезон %s (sportmonks_id=%s)",
                season.year if season else "—", new_season_data.get("name"), current_season_id,
            )
            season = importers.get_or_create_season(new_season_data, league, is_current=True)
            season_data = new_season_data

    if season is None or season_data is None:
        logger.warning("Sportmonks: не удалось определить активный сезон в ответе API — sportmonks_sync_season пропущен")
        return

    date_from = (season_data.get("starting_at") or "")[:10]
    date_to = (season_data.get("ending_at") or "")[:10]
    if not date_from or not date_to:
        return

    # Дробим на куски по 90 дней — тот же лимит API (100 дней), что и в
    # sync_sportmonks_season management command (см. его докстринг). Импорт
    # _chunk_date_range уже сделан выше вместе с _resolve_current_season_id.
    fixtures_by_id: dict = {}
    for chunk_from, chunk_to in _chunk_date_range(date_from, date_to):
        try:
            chunk_fixtures = client.get_fixtures_between(
                chunk_from, chunk_to, include="state;scores"
            )
        except SportmonksAPIError as exc:
            logger.error("Sportmonks: sportmonks_sync_season — ошибка календаря %s..%s: %s", chunk_from, chunk_to, exc)
            continue
        for fx in chunk_fixtures:
            fixtures_by_id[fx["id"]] = fx

    synced = 0
    for sm_id, fx in fixtures_by_id.items():
        state = (fx.get("state") or {}).get("developer_name") or ""
        mapped_status = importers.STATE_MAP.get(state, "scheduled")

        home_score = away_score = None
        for score in fx.get("scores") or []:
            if score.get("description") != "CURRENT":
                continue
            score_obj = score.get("score") or {}
            if score_obj.get("participant") == "home":
                home_score = score_obj.get("goals")
            elif score_obj.get("participant") == "away":
                away_score = score_obj.get("goals")

        existing = Match.objects.filter(sportmonks_id=str(sm_id)).only(
            "id", "status", "home_score", "away_score"
        ).first()

        changed = (
            existing is None
            or existing.status != mapped_status
            or existing.home_score != home_score
            or existing.away_score != away_score
        )
        if not changed:
            continue

        if _heavy_sync_fixture(client, league, season, sm_id):
            synced += 1

    logger.info(
        "Sportmonks: sportmonks_sync_season — в календаре %d матчей, синкнуто изменившихся: %d",
        len(fixtures_by_id), synced,
    )


@shared_task(bind=True, max_retries=1)
def sportmonks_sync_sidelined(self):
    """Раз в сутки (см. CELERY_BEAT_SCHEDULE, фаза 5 плана) — травмы и
    дисквалификации игроков, под бейдж "недоступен" на карточке игрока
    (players/models.py::PlayerSidelined). Один вызов client.get_sidelined()
    НА КОМАНДУ (16 команд лиги — 16 лёгких запросов раз в сутки, далеко от
    лимита, в отличие от live-опроса тут не нужна двухуровневая схема,
    сама по себе задача уже дешёвая)."""
    league, season = _get_league_and_season()
    if league is None or season is None:
        logger.debug("Sportmonks: нет активной лиги/сезона — sportmonks_sync_sidelined пропущен")
        return

    teams = Team.objects.filter(sportmonks_id__isnull=False, teamseason__season=season).distinct()
    if not teams:
        # Фоллбэк — TeamSeason ещё не проставлен (например, самый первый
        # прогон сразу после активации сезона до первого матча) — берём все
        # команды лиги с известным sportmonks_id, лучше лишний дешёвый
        # запрос, чем молча пропустить всю задачу.
        teams = Team.objects.filter(sportmonks_id__isnull=False)

    client = SportmonksClient()
    total_saved = 0
    for team in teams:
        try:
            sidelined_data = client.get_sidelined(int(team.sportmonks_id))
        except (SportmonksAPIError, ValueError) as exc:
            logger.error("Sportmonks: sportmonks_sync_sidelined — команда %s: %s", team, exc)
            continue
        total_saved += importers.import_sidelined(team, sidelined_data)

    logger.info("Sportmonks: sportmonks_sync_sidelined — обработано команд: %d, активных записей: %d", len(teams), total_saved)


@shared_task(bind=True, max_retries=1)
def sportmonks_sync_coach_activity(self):
    """Раз в сутки (см. CELERY_BEAT_SCHEDULE) — гигиена Coach.is_active/team,
    БЕЗ обращения к API (см. coaches/services.py::refresh_coach_activity —
    там же полное объяснение проблемы: Sportmonks не сообщает явно "тренер
    уволен", запись просто замирает на последнем известном состоянии, если
    тренер перестал появляться в новых fixture)."""
    from coaches.services import refresh_coach_activity

    result = refresh_coach_activity()
    logger.info(
        "Sportmonks: sportmonks_sync_coach_activity — команд проверено: %d, деактивировано: %d, реактивировано: %d",
        result["teams_checked"], result["deactivated"], result["reactivated"],
    )


@shared_task(bind=True, max_retries=1)
def sportmonks_health_check(self):
    """Раз в несколько часов (см. CELERY_BEAT_SCHEDULE) — лёгкий пинг для
    видимости в staff-дашборде (не критично для работы, в отличие от
    health_check_kff_api у KFF — Sportmonks не банит по TLS-отпечатку, но
    полезно раньше заметить проблему с токеном/квотой, чем по жалобе
    пользователя)."""
    client = SportmonksClient()
    try:
        league_data = client.get_league()
        logger.info("Sportmonks: health check OK — лига %s (id=%s)", league_data.get("name"), league_data.get("id"))
    except SportmonksAPIError as exc:
        logger.error("Sportmonks: health check FAILED — %s", exc)
