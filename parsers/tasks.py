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

# parsers/tasks.py

@shared_task(bind=True, max_retries=3, rate_limit='30/m')
def update_match_statuses(self):
    """
    Полная синхронизация незавершённых матчей с внешним API.
    
    Что обновляется:
    - Статус матча (scheduled → live → finished)
    - Счёт (home_score, away_score)
    - Время окончания (end_time)
    - События матча (голы, карточки, замены, VAR)
    - Составы (если ещё не загружены)
    - Статистика матча
    
    Запускается каждые 2-5 минут для live-матчей, каждые 10-15 мин для scheduled.
    """
    from matches.models import Match
    from events.models import MatchEvent
    from lineups.models import MatchLineup, MatchLineupPlayer
    from players.models import Player
    from teams.models import Team
    from django.utils import timezone
    from datetime import timedelta
    from parsers.kff.client import KFFClient
    from parsers.kff.importers import (
        import_events_and_minutes,
        import_lineups,
        import_coaches,
        import_stats,
        STATUS_MAP,
        EVENT_TYPE_MAP,
    )
    import logging
    
    logger = logging.getLogger(__name__)
    logger.info("🔄 Starting match status & data sync...")
    
    now = timezone.now()
    client = KFFClient()
    
    # === ШАГ 1: Получаем все незавершённые матчи ===
    active_matches = Match.objects.filter(
        status__in=['scheduled', 'live']
    ).select_related(
        'home_team', 'away_team', 'season', 'league', 'stadium'
    ).prefetch_related(
        'events',
        'lineups__players__player'
    )
    
    stats = {
        'total': active_matches.count(),
        'updated': 0,
        'unchanged': 0,
        'errors': 0,
        'new_events': 0,
        'status_changes': 0,
    }
    
    for match in active_matches:
        try:
            # Определяем tournament_code из сезона или используем дефолт
            tournament_code = getattr(match.season, 'tournament_code', 'pl')
            
            # === ШАГ 2: Запрашиваем данные матча из API ===
            game_data = client.get_game_details(match.external_id, tournament_code=tournament_code)
            if not game_data:
                logger.warning(f"⚠️ No data for match {match.external_id} from API")
                stats['errors'] += 1
                continue
            
            # === ШАГ 3: Обновляем базовые данные матча ===
            updated_fields = []
            
            # Статус
            api_status = game_data.get('status', 'scheduled')
            new_status = STATUS_MAP.get(api_status, match.status)
            if new_status != match.status:
                match.status = new_status
                updated_fields.append('status')
                stats['status_changes'] += 1
                logger.info(f"📊 Match {match.id}: {match.status} → {new_status}")
            
            # Счёт (важно: 0:0 — валидный счёт!)
            api_home_score = game_data.get('home_score')
            api_away_score = game_data.get('away_score')
            
            if api_home_score is not None and match.home_score != api_home_score:
                match.home_score = api_home_score
                updated_fields.append('home_score')
            
            if api_away_score is not None and match.away_score != api_away_score:
                match.away_score = api_away_score
                updated_fields.append('away_score')
            
            # Время окончания (если матч завершён)
            if new_status == 'finished' and not match.end_time:
                # Пытаемся получить из API или вычисляем
                api_end_time = game_data.get('end_time') or game_data.get('finished_at')
                if api_end_time:
                    from parsers.kff.importers import parse_match_datetime
                    match.end_time = parse_match_datetime(
                        api_end_time.split('T')[0] if 'T' in str(api_end_time) else api_end_time,
                        None,
                        tz=timezone.get_current_timezone()
                    )
                else:
                    match.end_time = match.start_time + timedelta(minutes=110)
                updated_fields.append('end_time')
            
            # Голосование открывается только для finished матчей
            if new_status == 'finished' and not match.voting_open_until:
                match.voting_open_until = match.start_time + timedelta(hours=48)
                updated_fields.append('voting_open_until')
            
            # has_lineup
            if game_data.get('has_lineup') and not match.has_lineup:
                match.has_lineup = True
                updated_fields.append('has_lineup')
            
            # Сохраняем изменения, если есть
            if updated_fields:
                updated_fields.append('updated_at')
                match.save(update_fields=updated_fields)
                stats['updated'] += 1
                logger.debug(f"✅ Match {match.id} updated: {updated_fields}")
            else:
                stats['unchanged'] += 1
            
            # === ШАГ 4: Синхронизация событий матча ===
            events_data = client.get_events(match.external_id, tournament_code=tournament_code)
            if events_data and events_data.get('events'):
                # Проверяем, есть ли новые события
                existing_event_ids = set(
                    MatchEvent.objects.filter(match=match).values_list('id', flat=True)
                )
                api_events = events_data.get('events', [])
                
                # Фильтруем только новые события (по комбинации минут+тип+игрок)
                new_events = []
                for evt in api_events:
                    minute = evt.get('minute')
                    event_type = evt.get('event_type', '').lower()
                    player_id = evt.get('player_id')
                    
                    # Простая проверка на дубликаты
                    exists = MatchEvent.objects.filter(
                        match=match,
                        minute=minute,
                        event_type__icontains=event_type.split('_')[0] if '_' in event_type else event_type,
                    ).exists()
                    
                    if not exists:
                        new_events.append(evt)
                
                if new_events:
                    # Импортируем новые события
                    if import_events_and_minutes(match, {'events': new_events}):
                        stats['new_events'] += len(new_events)
                        logger.info(f"⚡ Added {len(new_events)} new events for match {match.id}")
            
            # === ШАГ 5: Загрузка составов (если ещё нет) ===
            if match.has_lineup and not match.lineups.exists():
                lineup_data = client.get_lineup(match.external_id, tournament_code=tournament_code)
                if lineup_data:
                    if import_coaches(match, lineup_data):
                        logger.info(f"👨‍💼 Coaches imported for match {match.id}")
                    if import_lineups(match, lineup_data):
                        logger.info(f"👥 Lineups imported for match {match.id}")
            
            # === ШАГ 6: Статистика матча (опционально) ===
            if match.status == 'finished':
                stats_data = client.get_stats(match.external_id, tournament_code=tournament_code)
                if stats_data:
                    import_stats(match, stats_data)
                    match.stats_imported = True  # если добавите такое поле
                    match.save(update_fields=['stats_imported', 'updated_at'])
            
        except Exception as e:
            logger.error(f"❌ Error syncing match {match.id}: {type(e).__name__}: {e}", exc_info=True)
            stats['errors'] += 1
            # Не прерываем цикл — продолжаем с другими матчами
            continue
    
    # === ФИНАЛЬНЫЙ ЛОГ ===
    logger.info(f"🏁 Match sync completed: {stats}")
    
    # Если есть ошибки — можно отправить алерт
    if stats['errors'] > stats['total'] * 0.3:  # >30% ошибок
        from parsers.tasks import _send_sync_error_alert
        _send_sync_error_alert(
            f"Высокий процент ошибок при синхронизации матчей: {stats['errors']}/{stats['total']}",
            'match_sync_errors',
            extra_data=stats
        )
    
    return stats

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