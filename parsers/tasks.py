# parsers/tasks.py
from celery import shared_task
from django.utils import timezone
from parsers.kff.client import KFFClient
from parsers.kff.pipeline import sync_season, import_full_match
import logging

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3)
def sync_kff_premier_league(self, year: int = None):
    """Периодическая синхронизация Премьер-Лиги (авто-поиск сезона)"""
    logger.info(f"🔄 Starting Premier League sync (year={year})")
    
    client = KFFClient()
    season_id = client.find_premier_league_season(year=year)
    
    if not season_id:
        error_msg = f"❌ Could not find Premier League season (year={year})"
        logger.error(error_msg)
        return {'success': 0, 'failed': 0, 'error': error_msg}
    
    logger.info(f"✅ Syncing Premier League season {season_id} (year={year})")
    
    try:
        result = sync_season(season_id=season_id, tournament_code='pl', auto_detect=False)
        logger.info(f"✅ Premier League sync completed: {result}")
        return result
    except Exception as e:
        logger.error(f"❌ Premier League sync failed: {type(e).__name__}: {e}", exc_info=True)
        return {'success': 0, 'failed': 0, 'error': str(e)}


@shared_task(bind=True, max_retries=3)
def sync_recent_matches(self, season_id: int = None, limit: int = 10, tournament_code: str = None):
    """Синхронизация последних ЗАВЕРШЁННЫХ матчей (сортировка по дате)"""
    if tournament_code is None:
        tournament_code = KFFClient.TARGET_TOURNAMENT
    
    logger.info(f"🔄 Syncing recent finished matches (limit={limit}, season_id={season_id}, tournament={tournament_code})")
    
    client = KFFClient()
    
    # ✅ Получаем последние N завершённых матчей (с сортировкой по дате)
    recent_ids = client.get_recent_finished_matches(
        season_id=season_id,
        limit=limit,
        tournament_code=tournament_code
    )
    
    if not recent_ids:
        logger.warning(f"⚠️  No recent finished matches found")
        return {'success': 0, 'total': 0, 'error': 'No finished matches'}
    
    success = 0
    for mid in recent_ids:
        try:
            if import_full_match(mid, season_id, tournament_code=tournament_code):
                success += 1
        except Exception as e:
            logger.error(f"❌ Failed to import match {mid}: {type(e).__name__}: {e}")
    
    logger.info(f"✅ Synced {success}/{len(recent_ids)} recent finished matches")
    return {'success': success, 'total': len(recent_ids), 'season_id': season_id, 'tournament_code': tournament_code}


@shared_task(bind=True, max_retries=3)
def sync_full_season(self, season_id: int = None, tournament_code: str = None):
    """ПОЛНАЯ синхронизация ВСЕХ матчей сезона"""
    if tournament_code is None:
        tournament_code = KFFClient.TARGET_TOURNAMENT
    
    logger.info(f"🚀 Starting FULL SEASON sync (season_id={season_id}, tournament={tournament_code})")
    
    client = KFFClient()
    
    # Авто-определение если не указан
    if season_id is None:
        if tournament_code == KFFClient.TARGET_TOURNAMENT:
            season_id = client.find_premier_league_season()
        else:
            seasons = client.get_tournament_seasons(tournament_code=tournament_code)
            season_id = seasons[0]['id'] if seasons else None
        
        if not season_id:
            logger.error(f"❌ Could not auto-detect season for tournament '{tournament_code}'")
            return {'success': 0, 'failed': 0, 'error': 'Season not found'}
    
    try:
        result = sync_season(season_id=season_id, tournament_code=tournament_code, auto_detect=False)
        logger.info(f"✅ Full season sync completed: {result}")
        return result
    except Exception as e:
        logger.error(f"❌ Full season sync failed: {type(e).__name__}: {e}", exc_info=True)
        return {'success': 0, 'failed': 0, 'error': str(e)}


@shared_task
def update_match_statuses():
    """Обновление статусов матчей (live/finished)"""
    from matches.models import Match
    now = timezone.now()
    
    # Завершённые матчи
    finished = Match.objects.filter(
        status='live',
        end_time__lt=now
    ).update(status='finished')
    
    # Матчи которые должны начаться
    scheduled = Match.objects.filter(
        status='scheduled',
        start_time__lte=now
    ).update(status='live')
    
    logger.info(f"✅ Updated {finished} to finished, {scheduled} to live")
    return {'finished': finished, 'live': scheduled}


@shared_task
def health_check_kff_api():
    """Проверка доступности KFF API"""
    client = KFFClient()
    try:
        response = client._get("/seasons", params={"tournament": client.TARGET_TOURNAMENT}, retries=1)
        if response:
            logger.info("✅ KFF API is reachable")
            return {'status': 'ok', 'api': 'reachable'}
        else:
            logger.warning("⚠️  KFF API returned empty response")
            return {'status': 'warning', 'api': 'empty_response'}
    except Exception as e:
        logger.error(f"❌ KFF API health check failed: {e}")
        return {'status': 'error', 'error': str(e)}