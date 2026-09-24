# parsers/sportmonks/tasks.py
"""Celery-задачи синхронизации с Sportmonks.

Двухуровневая схема экономии лимита:
1. Лёгкий опрос (sportmonks_update_live) — один bulk-вызов livescores на всю лигу,
   сравнение статуса/счёта/событий с базой.
2. Тяжёлая догрузка (_heavy_sync_fixture) — полный include по одному матчу,
   только для реально изменившихся.
Redis-лок на матч не даёт двум задачам догружать один матч одновременно.
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

# TTL лока матча — чтобы лок протух даже при падении воркера.
FIXTURE_SYNC_LOCK_TIMEOUT_SECONDS = 120

# Лок на всю live-задачу: при тике раз в 15 с медленный ответ API не должен
# приводить к наложению запусков.
LIVE_POLL_OVERLAP_LOCK_KEY = "sportmonks:update_live:running"
LIVE_POLL_OVERLAP_LOCK_TIMEOUT_SECONDS = 30

# За сколько часов до матча начинаем тянуть составы.
UPCOMING_WINDOW = timedelta(hours=3)


# Рубильник синка (PlatformSetting sportmonks_sync_enabled). Проверяется в начале
# каждой задачи — Beat тикает, но задача сразу выходит. Кнопка — на странице «Парсер».
def _sync_enabled() -> bool:
    """Включён ли синк (кэш настроек 60 с)."""
    from core.models import get_setting

    return bool(get_setting("sportmonks_sync_enabled", True))


def _record_sync_run(task_name: str, started_at, total: int = 0, updated: int = 0, unchanged: int = 0, errors: int = 0) -> None:
    """Пишет ParserSyncRun(source='sportmonks') для дашборда. Ошибка записи не ломает синк."""
    try:
        ParserSyncRun.objects.create(
            task_name=task_name, source="sportmonks", started_at=started_at,
            total=total, updated=updated, unchanged=unchanged, errors=errors,
        )
    except Exception:
        logger.error("Sportmonks: не удалось записать ParserSyncRun для %s", task_name, exc_info=True)


def _get_league_and_season():
    """(лига, активный сезон) или None — задачи тогда тихо выходят."""
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
    """Тяжёлая догрузка одного матча. False — лок занят или запрос упал."""
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


def _sportmonks_update_live_impl(self):
    """Лёгкий live-опрос: один bulk-вызов на лигу (включая events).

    Тяжёлая догрузка — для матчей, где изменились статус, счёт или набор событий.
    Матчи, которые у нас ещё live, но пропали из ответа API (закончились), тоже
    догружаются — иначе они зависали бы в live. ParserSyncRun пишется на каждом
    запуске, чтобы дашборд видел, что синк жив.
    """
    if not _sync_enabled():
        logger.info("Sportmonks: синк выключен флагом sportmonks_sync_enabled — sportmonks_update_live пропущен")
        return
    started_at = timezone.now()
    league, season = _get_league_and_season()
    if league is None or season is None:
        logger.debug("Sportmonks: нет активной лиги/сезона — sportmonks_update_live пропущен")
        return

    client = SportmonksClient()
    league_sm_id = int(league.sportmonks_id)
    try:
        # events нужны для детекции карточек/замен/VAR — лимит не увеличивают.
        live_fixtures = client.get_livescores(
            include="state;participants;scores;events", league_id=league_sm_id
        )
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

        # Дополнительная проверка лиги фикстуры (фильтр inplay не гарантирован).
        # Если league_id нет — не пропускаем, но логируем.
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

        # Сравниваем подпись событий (id, developer_name): ловим новые карточки/замены,
        # отменённые голы и VAR-замену жёлтой на красную.
        if not changed and existing is not None:
            api_signature = {
                (str(e["id"]), (e.get("type") or {}).get("developer_name") or "")
                for e in (fx.get("events") or []) if e.get("id") is not None
            }
            local_signature = {
                (ev.sportmonks_id, (ev.extra_data or {}).get("type", {}).get("developer_name") or "")
                for ev in existing.events.exclude(sportmonks_id__isnull=True).only(
                    "sportmonks_id", "extra_data"
                )
            }
            if api_signature != local_signature:
                changed = True

        if not changed:
            continue

        if _heavy_sync_fixture(client, league, season, sm_id):
            synced += 1
        else:
            errors += 1

    # Матчи, выпавшие из live-списка. manual_override не трогаем.
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
def sportmonks_update_live(self):
    """Обёртка с Redis-локом от наложения тиков; логика — в _sportmonks_update_live_impl."""
    if not cache.add(
        LIVE_POLL_OVERLAP_LOCK_KEY, "1", timeout=LIVE_POLL_OVERLAP_LOCK_TIMEOUT_SECONDS
    ):
        logger.info(
            "Sportmonks: sportmonks_update_live уже выполняется другим тиком, "
            "пропуск (overlap-лок, см. её докстринг)"
        )
        return
    try:
        return _sportmonks_update_live_impl(self)
    finally:
        cache.delete(LIVE_POLL_OVERLAP_LOCK_KEY)


# Окно после финального свистка, в котором досинкиваем статистику:
# поставщик часто дозаполняет её с задержкой, а лёгкий опрос после
# совпадения счёта и статуса матч больше не трогает.
STATS_RESYNC_WINDOW = timedelta(hours=3)


@shared_task(bind=True, max_retries=2)
def sportmonks_resync_recent_stats(self):
    """Каждые 15 минут — безусловная тяжёлая догрузка матчей, завершившихся в пределах STATS_RESYNC_WINDOW."""
    if not _sync_enabled():
        logger.info("Sportmonks: синк выключен флагом sportmonks_sync_enabled — sportmonks_resync_recent_stats пропущен")
        return
    league, season = _get_league_and_season()
    if league is None or season is None:
        logger.debug("Sportmonks: нет активной лиги/сезона — sportmonks_resync_recent_stats пропущен")
        return

    started_at = timezone.now()
    cutoff = started_at - STATS_RESYNC_WINDOW
    recent_finished = list(Match.objects.filter(
        league=league, season=season, status="finished",
        sportmonks_id__isnull=False, start_time__gte=cutoff,
    ).only("id", "sportmonks_id"))

    if not recent_finished:
        _record_sync_run("sportmonks_resync_recent_stats", started_at, total=0)
        return

    client = SportmonksClient()
    synced = errors = 0
    for match in recent_finished:
        if _heavy_sync_fixture(client, league, season, match.sportmonks_id):
            synced += 1
        else:
            errors += 1

    logger.info(
        "Sportmonks: sportmonks_resync_recent_stats — досинкано статистики %d матч(ей), ошибок %d "
        "(окно %s после финального свистка)",
        synced, errors, STATS_RESYNC_WINDOW,
    )
    _record_sync_run(
        "sportmonks_resync_recent_stats", started_at,
        total=len(recent_finished), updated=synced, errors=errors,
    )


@shared_task(bind=True, max_retries=2)
def sportmonks_update_upcoming(self):
    """Составы для матчей в ближайшие UPCOMING_WINDOW часов без состава."""
    if not _sync_enabled():
        logger.info("Sportmonks: синк выключен флагом sportmonks_sync_enabled — sportmonks_update_upcoming пропущен")
        return
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
    """Суточная сверка календаря: новые матчи и матчи с расхождением статуса/счёта.
    Если у Sportmonks сменился текущий сезон — создаём и активируем его сами.
    """
    if not _sync_enabled():
        logger.info("Sportmonks: синк выключен флагом sportmonks_sync_enabled — sportmonks_sync_season пропущен")
        return
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

    # Куски по 90 дней (лимит API — 100 дней).
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
    """Травмы и дисквалификации — раз в сутки, один запрос на команду.
    Пишет ParserSyncRun, чтобы прогон был виден на дашборде.
    """
    if not _sync_enabled():
        logger.info("Sportmonks: синк выключен флагом sportmonks_sync_enabled — sportmonks_sync_sidelined пропущен")
        return
    started_at = timezone.now()
    league, season = _get_league_and_season()
    if league is None or season is None:
        logger.debug("Sportmonks: нет активной лиги/сезона — sportmonks_sync_sidelined пропущен")
        _record_sync_run("sportmonks_sync_sidelined", started_at, total=0)
        return

    teams = Team.objects.filter(sportmonks_id__isnull=False, teamseason__season=season).distinct()
    if not teams:
        # Нет TeamSeason — берём все команды лиги с sportmonks_id.
        teams = Team.objects.filter(sportmonks_id__isnull=False)

    client = SportmonksClient()
    total_saved = 0
    errors = 0
    for team in teams:
        try:
            sidelined_data = client.get_sidelined(int(team.sportmonks_id))
        except (SportmonksAPIError, ValueError) as exc:
            logger.error("Sportmonks: sportmonks_sync_sidelined — команда %s: %s", team, exc)
            errors += 1
            continue
        total_saved += importers.import_sidelined(team, sidelined_data)

    logger.info("Sportmonks: sportmonks_sync_sidelined — обработано команд: %d, активных записей: %d", len(teams), total_saved)
    _record_sync_run(
        "sportmonks_sync_sidelined", started_at,
        total=len(teams), updated=total_saved, errors=errors,
    )


@shared_task(bind=True, max_retries=1)
def sportmonks_sync_coach_activity(self):
    """Актуализация Coach.is_active/team без запросов к API (coaches/services.py)."""
    from coaches.services import refresh_coach_activity

    result = refresh_coach_activity()
    logger.info(
        "Sportmonks: sportmonks_sync_coach_activity — команд проверено: %d, деактивировано: %d, реактивировано: %d",
        result["teams_checked"], result["deactivated"], result["reactivated"],
    )


@shared_task(bind=True, max_retries=1)
def sportmonks_health_check(self):
    """Плановый health-check токена/квоты. Подчиняется рубильнику синка;
    ручная кнопка «Проверить доступность» — нет.
    """
    if not _sync_enabled():
        logger.info("Sportmonks: синк выключен флагом sportmonks_sync_enabled — sportmonks_health_check пропущен")
        return
    client = SportmonksClient()
    try:
        league_data = client.get_league()
        logger.info("Sportmonks: health check OK — лига %s (id=%s)", league_data.get("name"), league_data.get("id"))
    except SportmonksAPIError as exc:
        logger.error("Sportmonks: health check FAILED — %s", exc)
