# parsers/tasks.py
from celery import shared_task
from django.utils import timezone
from django.conf import settings
from django.core.mail import send_mail
from django.template.loader import render_to_string
from parsers.kff.client import KFFClient
from parsers.kff.pipeline import sync_season, import_full_match
import logging
from datetime import timedelta

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
        _send_sync_error_alert(error_msg, 'season_detection')
        return {'success': 0, 'failed': 0, 'error': error_msg}
    
    logger.info(f"✅ Syncing Premier League season {season_id} (year={year})")
    
    try:
        result = sync_season(season_id=season_id, tournament_code='pl', auto_detect=False)
        logger.info(f"✅ Premier League sync completed: {result}")
        
        if result.get('failed', 0) > 0:
            _send_sync_error_alert(
                f"Синхронизация завершена с ошибками: {result['failed']} из {result['total']}",
                'sync_errors',
                extra_data=result
            )
        
        return result
    except Exception as e:
        error_msg = f"❌ Premier League sync failed: {type(e).__name__}: {e}"
        logger.error(error_msg, exc_info=True)
        _send_sync_error_alert(error_msg, 'sync_critical', extra_data={'exception': str(e)})
        return {'success': 0, 'failed': 0, 'error': str(e)}

@shared_task(bind=True, max_retries=3)
def sync_recent_matches(self, season_id: int = None, limit: int = 10, tournament_code: str = None):
    """Синхронизация последних ЗАВЕРШЁННЫХ матчей (сортировка по дате)"""
    if tournament_code is None:
        tournament_code = KFFClient.TARGET_TOURNAMENT
    
    logger.info(f"🔄 Syncing recent finished matches (limit={limit}, season_id={season_id}, tournament={tournament_code})")
    
    client = KFFClient()
    
    # ✅ Авто-определение сезона если не указан
    if season_id is None:
        season_id = client.find_premier_league_season()
        if not season_id:
            error_msg = f"❌ Could not auto-detect season for tournament {tournament_code}"
            logger.error(error_msg)
            _send_sync_error_alert(error_msg, 'season_detection')
            return {'success': 0, 'total': 0, 'error': error_msg}
    
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
    failed = 0
    failed_matches = []
    
    for mid in recent_ids:
        try:
            if import_full_match(mid, season_id, tournament_code=tournament_code):
                success += 1
            else:
                failed += 1
                failed_matches.append(mid)
        except Exception as e:
            logger.error(f"❌ Failed to import match {mid}: {type(e).__name__}: {e}")
            failed += 1
            failed_matches.append(mid)
    
    logger.info(f"✅ Synced {success}/{len(recent_ids)} recent finished matches")
    
    if failed > 0:
        _send_sync_error_alert(
            f"Ошибки при синхронизации {failed} матчей: {failed_matches[:5]}",
            'sync_errors',
            extra_data={'failed_matches': failed_matches, 'success': success}
        )
    
    return {
        'success': success,
        'total': len(recent_ids),
        'failed': failed,
        'season_id': season_id,
        'tournament_code': tournament_code
    }

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
            error_msg = f"❌ Could not auto-detect season for tournament '{tournament_code}'"
            logger.error(error_msg)
            _send_sync_error_alert(error_msg, 'season_detection')
            return {'success': 0, 'failed': 0, 'error': 'Season not found'}
    
    try:
        result = sync_season(season_id=season_id, tournament_code=tournament_code, auto_detect=False)
        logger.info(f"✅ Full season sync completed: {result}")
        
        if result.get('failed', 0) > 0:
            _send_sync_error_alert(
                f"Полная синхронизация завершена с ошибками: {result['failed']} из {result['total']}",
                'sync_errors',
                extra_data=result
            )
        
        return result
    except Exception as e:
        error_msg = f"❌ Full season sync failed: {type(e).__name__}: {e}"
        logger.error(error_msg, exc_info=True)
        _send_sync_error_alert(error_msg, 'sync_critical', extra_data={'exception': str(e)})
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
        error_msg = f"❌ KFF API health check failed: {e}"
        logger.error(error_msg)
        return {'status': 'error', 'error': str(e)}

@shared_task
def check_sync_errors_and_alert():
    """
    ✅ НОВОЕ: Проверка ошибок синхронизации за последние 24 часа
    и отправка алерта если критическое количество ошибок
    """
    from django.db.models import Count, Q
    from matches.models import Match
    
    now = timezone.now()
    cutoff = now - timedelta(hours=24)
    
    # Считаем матчи без составов (признак проблемы импорта)
    matches_without_lineups = Match.objects.filter(
        status='finished',
        created_at__gte=cutoff,
        has_lineup=False
    ).count()
    
    # Считаем матчи без событий
    from events.models import MatchEvent
    matches_without_events = Match.objects.filter(
        status='finished',
        created_at__gte=cutoff
    ).exclude(
        id__in=MatchEvent.objects.filter(
            created_at__gte=cutoff
        ).values_list('match_id', flat=True)
    ).count()
    
    threshold_lineups = 5  # Если больше 5 матчей без составов — алерт
    threshold_events = 10  # Если больше 10 матчей без событий — алерт
    
    alerts = []
    
    if matches_without_lineups > threshold_lineups:
        alerts.append(f"⚠️ {matches_without_lineups} матчей без составов за 24ч")
    
    if matches_without_events > threshold_events:
        alerts.append(f"⚠️ {matches_without_events} матчей без событий за 24ч")
    
    if alerts:
        error_msg = "Проблемы с синхронизацией:\n" + "\n".join(alerts)
        logger.warning(error_msg)
        _send_sync_error_alert(error_msg, 'sync_monitoring', extra_data={
            'matches_without_lineups': matches_without_lineups,
            'matches_without_events': matches_without_events
        })
        return {'status': 'alert_sent', 'alerts': alerts}
    
    logger.info("✅ Sync monitoring: No critical issues detected")
    return {'status': 'ok'}

def _send_sync_error_alert(error_message: str, alert_type: str, extra_data: dict = None):
    """
    ✅ НОВОЕ: Отправка email-алерта админу при критических ошибках
    """
    if not getattr(settings, 'ENABLE_SYNC_ERROR_ALERTS', True):
        return
    
    admin_email = getattr(settings, 'ADMIN_ALERT_EMAIL', settings.CONTACT_EMAIL)
    site_url = getattr(settings, 'SITE_URL', 'https://dopx.kz')
    
    subject = f"🚨 DOPX Sync Alert [{alert_type}]"
    
    html_message = render_to_string('emails/sync_error_alert.html', {
        'error_message': error_message,
        'alert_type': alert_type,
        'extra_data': extra_data,
        'timestamp': timezone.now(),
        'site_url': site_url,
    })
    
    try:
        send_mail(
            subject=subject,
            message='',
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[admin_email],
            html_message=html_message,
            fail_silently=True,  # Не ломаем задачу если email не отправился
        )
        logger.info(f"✅ Sync error alert sent to {admin_email}")
    except Exception as e:
        logger.error(f"❌ Failed to send sync error alert: {e}")

@shared_task
def sync_all_enabled_tournaments():
    """
    ✅ НОВОЕ: Синхронизация всех включённых турниров из настроек
    Чтобы включить другие лиги — добавьте их в PARSER_SETTINGS.ENABLED_TOURNAMENTS
    """
    enabled_tournaments = getattr(settings, 'PARSER_SETTINGS', {}).get(
        'ENABLED_TOURNAMENTS',
        ['pl']
    )
    
    results = {}
    
    for tournament_code in enabled_tournaments:
        logger.info(f"🔄 Starting sync for tournament: {tournament_code}")
        
        try:
            if tournament_code == 'pl':
                result = sync_kff_premier_league.delay()
            else:
                result = sync_full_season.delay(tournament_code=tournament_code)
            
            results[tournament_code] = {'status': 'queued', 'task_id': result.id}
        except Exception as e:
            logger.error(f"❌ Failed to queue sync for {tournament_code}: {e}")
            results[tournament_code] = {'status': 'error', 'error': str(e)}
    
    return results