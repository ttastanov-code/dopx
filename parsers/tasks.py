# parsers/tasks.py — СОЗДАТЬ

from celery import shared_task
from django.utils import timezone
from parsers.kff.client import KFFClient
from parsers.kff.pipeline import sync_season, import_full_match
import logging

logger = logging.getLogger(__name__)


@shared_task
def sync_kff_season_periodic(season_id=200):
    """Периодическая синхронизация сезона"""
    logger.info(f"Starting periodic KFF sync for season {season_id}")
    
    try:
        result = sync_season(season_id)
        logger.info(f"KFF sync completed: {result}")
        return result
    except Exception as e:
        logger.error(f"KFF sync failed: {e}")
        return {'success': 0, 'failed': 0, 'error': str(e)}


@shared_task
def sync_recent_matches():
    """Синхронизация последних матчей"""
    client = KFFClient()
    
    # Получаем матчи сезона 200 (пример)
    match_ids = client.get_season_matches(200)
    
    # Берём последние 10
    recent_ids = match_ids[-10:] if len(match_ids) > 10 else match_ids
    
    success = 0
    for mid in recent_ids:
        try:
            if import_full_match(mid, 200):
                success += 1
        except Exception as e:
            logger.error(f"Failed to import match {mid}: {e}")
    
    logger.info(f"Synced {success}/{len(recent_ids)} recent matches")
    return {'success': success, 'total': len(recent_ids)}


@shared_task
def update_match_statuses():
    """Обновление статусов матчей"""
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
    
    logger.info(f"Updated {finished} to finished, {scheduled} to live")
    return {'finished': finished, 'live': scheduled}