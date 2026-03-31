# parsers/kff/pipeline.py
from .client import KFFClient
from .importers import (
    import_match_core,
    import_lineups,
    import_events_and_minutes,
    import_stats,
    import_coaches,
    get_or_create_season
)
import logging
from django.db import connection
from django.db.utils import IntegrityError
from django.utils import timezone

logger = logging.getLogger(__name__)

def import_full_match(match_id: int, season_id: int = None, tournament_code: str = None) -> bool:
    """Полный импорт одного матча с детальным логированием"""
    if tournament_code is None:
        tournament_code = KFFClient.TARGET_TOURNAMENT
    
    logger.info(f"🔄 Начало импорта матча #{match_id} (tournament={tournament_code})")
    start_time = timezone.now()
    
    client = KFFClient()
    stats = {
        'match_id': match_id,
        'start_time': start_time,
        'end_time': None,
        'duration': None,
        'success': False,
        'errors': [],
        'created': {},
        'updated': {}
    }
    
    try:
        # === 1. Основная информация о матче ===
        logger.info(f"  📊 Загрузка данных матча...")
        game_resp = client.get_game_details(match_id, tournament_code=tournament_code)
        if not game_resp:
            error = f"API вернул None для матча {match_id}"
            logger.error(f"  ❌ {error}")
            stats['errors'].append(error)
            return False
        
        if not isinstance(game_resp, dict):
            error = f"Неожиданный тип ответа: {type(game_resp)}"
            logger.error(f"  ❌ {error}")
            stats['errors'].append(error)
            return False
        
        game_data = game_resp
        if not season_id:
            season_id = game_data.get("season_id")
        
        # Импорт ядра матча
        try:
            match, created = _import_match_with_tracking(game_data, season_id)
            if created:
                stats['created']['match'] = str(match.id)
                logger.info(f"  ✅ Матч создан: {match.id}")
            else:
                stats['updated']['match'] = str(match.id)
                logger.info(f"  🔄 Матч обновлён: {match.id}")
            
            logger.info(f"  ⚽ {match.home_team} vs {match.away_team} ({match.status})")
            logger.info(f"  📅 Start: {match.start_time}, End: {match.end_time}")
            logger.info(f"  🗳️  Voting until: {match.voting_open_until}")
            if match.referee:
                logger.info(f"  🟨 Referee: {match.referee}")
                
        except IntegrityError as e:
            error = f"IntegrityError при импорте матча: {e}"
            logger.error(f"  ❌ {error}")
            stats['errors'].append(error)
            return False
        except Exception as e:
            error = f"Ошибка импорта матча: {type(e).__name__}: {e}"
            logger.error(f"  ❌ {error}", exc_info=True)
            stats['errors'].append(error)
            return False
        
        # === 2. Составы + Тренеры ===
        if game_data.get("has_lineup"):
            logger.info(f"  👥 Загрузка составов и тренеров...")
            lineup_resp = client.get_lineup(match_id, tournament_code=tournament_code)
            if lineup_resp and isinstance(lineup_resp, dict):
                lineup_data = lineup_resp.get("data", lineup_resp)
                
                logger.info(f"  📊 Lineup keys: {list(lineup_data.keys())}")
                
                if "lineups" in lineup_data:
                    for side, team_data in lineup_data["lineups"].items():
                        if team_data:
                            starters = team_data.get("starters", []) or team_data.get("starting_lineup", [])
                            subs = team_data.get("substitutes", []) or team_data.get("bench", [])
                            logger.info(f"  📊 {side}: {len(starters)} starters, {len(subs)} subs")
                
                # ✅ СОЗДАЁМ ТРЕНЕРОВ
                try:
                    if import_coaches(match, lineup_data):
                        logger.info(f"  ✅ Тренеры импортированы")
                        stats['created']['coaches'] = True
                    else:
                        logger.warning(f"  ⚠️  Нет данных тренеров")
                except Exception as e:
                    error = f"Ошибка импорта тренеров: {e}"
                    logger.warning(f"  ⚠️  {error}", exc_info=True)
                    stats['errors'].append(error)
                
                # Создаём составы
                try:
                    if import_lineups(match, lineup_data):
                        logger.info(f"  ✅ Составы импортированы")
                        stats['created']['lineups'] = True
                    else:
                        logger.warning(f"  ⚠️  Нет данных составов")
                except Exception as e:
                    error = f"Ошибка импорта составов: {e}"
                    logger.warning(f"  ⚠️  {error}", exc_info=True)
                    stats['errors'].append(error)
        
        # === 3. События матча ===
        logger.info(f"  ⚡ Загрузка событий...")
        events_resp = client.get_events(match_id, tournament_code=tournament_code)
        if events_resp and isinstance(events_resp, dict):
            events_data = events_resp.get("data", events_resp)
            events_list = events_data.get("events", []) if isinstance(events_data, dict) else []
            
            if events_list:
                try:
                    import_events_and_minutes(match, events_data)
                    event_count = len(events_list)
                    logger.info(f"  ✅ События импортированы ({event_count} events)")
                    stats['created']['events'] = event_count
                except Exception as e:
                    error = f"Ошибка импорта событий: {e}"
                    logger.warning(f"  ⚠️  {error}", exc_info=True)
                    stats['errors'].append(error)
            else:
                logger.info(f"  ℹ️  Нет событий")
        else:
            logger.warning(f"  ⚠️  No events data available")
        
        # === 4. Статистика матча ===
        logger.info(f"  📈 Загрузка статистики...")
        stats_resp = client.get_stats(match_id, tournament_code=tournament_code)
        if stats_resp and isinstance(stats_resp, dict):
            stats_data = stats_resp.get("data", stats_resp)
            if stats_data:
                try:
                    import_stats(match, stats_data)
                    logger.info(f"  ✅ Статистика импортирована")
                    stats['created']['stats'] = True
                except Exception as e:
                    error = f"Ошибка импорта статистики: {e}"
                    logger.warning(f"  ⚠️  {error}", exc_info=True)
                    stats['errors'].append(error)
            else:
                logger.info(f"  ℹ️  Нет статистики")
        
        stats['success'] = True
        logger.info(f"✅ Импорт матча #{match_id} завершён успешно")
        
    except Exception as e:
        error = f"Критическая ошибка импорта: {type(e).__name__}: {e}"
        logger.error(f"❌ {error}", exc_info=True)
        stats['errors'].append(error)
        stats['success'] = False
    
    finally:
        stats['end_time'] = timezone.now()
        stats['duration'] = (stats['end_time'] - stats['start_time']).total_seconds()
        _log_import_stats(stats)
    
    return stats['success']

def _import_match_with_tracking(game_data: dict, season_id: int = None):
    """Внутренняя обёртка: проверяет существование матча до импорта"""
    from matches.models import Match
    existing = Match.objects.filter(external_id=str(game_data.get("id"))).first()
    match = import_match_core(game_data, season_id)
    return match, existing is None

def _log_import_stats(stats: dict):
    """Логирование статистики импорта"""
    logger.info("=" * 60)
    logger.info(f"📊 СТАТИСТИКА ИМПОРТА")
    logger.info("=" * 60)
    logger.info(f"Матч ID: {stats['match_id']}")
    logger.info(f"Успех: {stats['success']}")
    logger.info(f"Длительность: {stats['duration']:.2f} сек")
    if stats['created']:
        logger.info(f"Создано: {stats['created']}")
    if stats['updated']:
        logger.info(f"Обновлено: {stats['updated']}")
    if stats['errors']:
        logger.warning(f"Ошибки: {stats['errors']}")
    logger.info("=" * 60)

def sync_season(season_id: int = None, tournament_code: str = None, match_ids: list = None, auto_detect: bool = True) -> dict:
    """
    Синхронизация всего сезона с авто-определением
    """
    if tournament_code is None:
        tournament_code = KFFClient.TARGET_TOURNAMENT
    
    client = KFFClient()
    
    # Авто-определение сезона если не указан
    if season_id is None and auto_detect:
        if tournament_code == KFFClient.TARGET_TOURNAMENT:
            season_id = client.find_premier_league_season()
        else:
            seasons = client.get_tournament_seasons(tournament_code=tournament_code)
            current = [s for s in seasons if s.get('is_current')]
            season_id = current[0]['id'] if current else (seasons[0]['id'] if seasons else None)
        
        if not season_id:
            logger.error(f"❌ Could not auto-detect season for tournament '{tournament_code}'")
            return {"success": 0, "failed": 0, "total": 0, "error": "Season not found"}
    
    logger.info(f"🚀 Начало синхронизации сезона {season_id} (tournament={tournament_code}, auto_detect={auto_detect})")
    start_time = timezone.now()
    
    if not match_ids:
        logger.info(f"🔍 Поиск матчей сезона...")
        match_ids = client.get_season_matches(season_id, tournament_code=tournament_code, auto_detect=False)
        logger.info(f"✅ Найдено матчей: {len(match_ids)}")
    
    if not match_ids:
        logger.warning(f"⚠️  Матчи не найдены для сезона {season_id} (tournament={tournament_code})")
        return {"success": 0, "failed": 0, "total": 0}
    
    results = {"success": 0, "failed": 0, "total": len(match_ids)}
    failed_matches = []
    
    for i, mid in enumerate(match_ids, 1):
        logger.info(f"[{i}/{len(match_ids)}] Матч #{mid}")
        try:
            if import_full_match(mid, season_id, tournament_code=tournament_code):
                results["success"] += 1
            else:
                results["failed"] += 1
                failed_matches.append(mid)
        except Exception as e:
            logger.error(f"❌ Критическая ошибка для матча {mid}: {type(e).__name__}: {e}", exc_info=True)
            results["failed"] += 1
            failed_matches.append(mid)
    
    # Итоговая статистика
    duration = (timezone.now() - start_time).total_seconds()
    logger.info("=" * 60)
    logger.info(f"🏁 СИНХРОНИЗАЦИЯ СЕЗОНА ЗАВЕРШЕНА")
    logger.info("=" * 60)
    logger.info(f"Всего: {results['total']}")
    logger.info(f"✅ Успешно: {results['success']}")
    logger.info(f"❌ Ошибки: {results['failed']}")
    logger.info(f"⏱️  Длительность: {duration:.2f} сек")
    if match_ids:
        logger.info(f"📊 Средняя скорость: {duration/len(match_ids):.2f} сек/матч")
    if failed_matches:
        logger.warning(f"⚠️  Неудачные матчи: {failed_matches}")
    logger.info("=" * 60)
    
    return results