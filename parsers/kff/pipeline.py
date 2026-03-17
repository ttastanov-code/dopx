from .client import KFFClient
from .importers import (
    import_match_core,
    import_lineups,
    import_events_and_minutes,
    import_stats,
    get_or_create_season
)
import logging

logger = logging.getLogger(__name__)


def import_full_match(match_id: int, season_id: int = None) -> bool:
    """Полный импорт одного матча"""
    print(f"  → Processing match {match_id}...")
    
    client = KFFClient()
    
    # 1. Основная информация
    game_resp = client.get_game_details(match_id)
    
    if game_resp is None:
        logger.error(f"❌ API returned None for match {match_id}")
        return False
    
    if not isinstance(game_resp, dict):
        logger.error(f"❌ Unexpected response type for match {match_id}: {type(game_resp)}")
        return False
    
    if "id" not in game_resp and "home_team" not in game_resp:
        logger.error(f"❌ Response missing required fields for match {match_id}. Keys: {list(game_resp.keys())[:10]}")
        return False
    
    game_data = game_resp
    
    if not season_id:
        season_id = game_data.get("season_id")
    
    try:
        match = import_match_core(game_data, season_id)
        print(f"    ✓ Match: {match.home_team} vs {match.away_team} ({game_data.get('status')})")
        print(f"    ✓ Start: {match.start_time}, End: {match.end_time}")
        print(f"    ✓ Voting until: {match.voting_open_until}")
        print(f"    ✓ Referee: {match.referee}")
    except Exception as e:
        logger.error(f"❌ Error importing match core: {type(e).__name__}: {e}", exc_info=True)
        return False
    
    # 2. Составы
    if game_data.get("has_lineup"):
        lineup_resp = client.get_lineup(match_id)
        if lineup_resp and isinstance(lineup_resp, dict):
            lineup_data = lineup_resp.get("data", lineup_resp)
            try:
                import_lineups(match, lineup_data)
                print(f"    ✓ Lineups imported")
            except Exception as e:
                logger.warning(f"⚠ Lineup import warning: {e}")
        else:
            print(f"    ⚠ No lineup data available")
    
    # 3. События
    events_resp = client.get_events(match_id)
    if events_resp and isinstance(events_resp, dict):
        events_data = events_resp.get("data", events_resp)
        if events_data.get("events"):
            try:
                import_events_and_minutes(match, events_data)
                event_count = len(events_data["events"])
                print(f"    ✓ Events imported ({event_count} events)")
            except Exception as e:
                logger.warning(f"⚠ Events import warning: {e}")
    
    # 4. Статистика
    stats_resp = client.get_stats(match_id)
    if stats_resp and isinstance(stats_resp, dict):
        stats_data = stats_resp.get("data", stats_resp)
        if stats_data:
            try:
                import_stats(match, stats_data)
                print(f"    ✓ Stats imported")
            except Exception as e:
                logger.warning(f"⚠ Stats import warning: {e}")
    
    return True


def sync_season(season_id: int, match_ids: list = None) -> dict:
    """Синхронизация всего сезона"""
    client = KFFClient()
    
    if not match_ids:
        print(f"Discovering matches for season {season_id}...")
        match_ids = client.get_season_matches(season_id)
        print(f"Found {len(match_ids)} matches")
    
    if not match_ids:
        return {"success": 0, "failed": 0, "total": 0}
    
    results = {"success": 0, "failed": 0, "total": len(match_ids)}
    
    for i, mid in enumerate(match_ids, 1):
        print(f"[{i}/{len(match_ids)}] Match #{mid}")
        try:
            if import_full_match(mid, season_id):
                results["success"] += 1
            else:
                results["failed"] += 1
        except Exception as e:
            logger.error(f"❌ Critical error for match {mid}: {type(e).__name__}: {e}", exc_info=True)
            results["failed"] += 1
    
    return results