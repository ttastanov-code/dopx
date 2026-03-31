from django.core.management.base import BaseCommand
from parsers.kff.client import KFFClient
from parsers.kff.pipeline import sync_season, import_full_match
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Sync matches from KFF League API with tournament code support"
    
    def add_arguments(self, parser):
        parser.add_argument(
            '--season',
            type=int,
            help='Season ID to sync (если не указан — авто-поиск)'
        )
        parser.add_argument(
            '--year',
            type=int,
            help='Year for auto-detection (2024, 2025, 2026)'
        )
        parser.add_argument(
            '--tournament',
            type=str,
            choices=list(KFFClient.TOURNAMENT_CODES.keys()),
            default=KFFClient.TARGET_TOURNAMENT,
            help=f'Tournament code (default: {KFFClient.TARGET_TOURNAMENT} for Premier League). '
                 f'Available: {", ".join(KFFClient.TOURNAMENT_CODES.keys())}'
        )
        parser.add_argument(
            '--match-id',
            type=int,
            help='Sync a specific match ID only'
        )
        parser.add_argument(
            '--match-ids',
            type=str,
            help='Comma-separated list of match IDs'
        )
        parser.add_argument(
            '--limit',
            type=int,
            default=10,
            help='Limit for recent matches (default: 10)'
        )
        parser.add_argument(
            '--recent-only',
            action='store_true',
            help='Sync only recent FINISHED matches (sorted by date)'
        )
        parser.add_argument(
            '--full',
            action='store_true',
            help='Force full season sync'
        )
        parser.add_argument(
            '--no-auto-detect',
            action='store_true',
            help='Disable auto-detection of season'
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be imported without saving'
        )
        parser.add_argument(
            '--debug-api',
            action='store_true',
            help='Print raw API responses for debugging'
        )
    
    def handle(self, *args, **options):
        dry_run = options['dry_run']
        debug_api = options['debug_api']
        no_auto_detect = options['no_auto_detect']
        tournament_code = options['tournament']
        
        if dry_run:
            self.stdout.write(self.style.WARNING("🔍 DRY RUN MODE"))
        
        # === Конкретный матч ===
        if options['match_id']:
            match_id = options['match_id']
            season_id = options['season']
            self.stdout.write(f"Importing match #{match_id} (tournament={tournament_code})...")
            
            if dry_run:
                client = KFFClient()
                details = client.get_game_details(match_id, tournament_code=tournament_code)
                if details:
                    self.stdout.write(f"  ✅ Found: {details.get('home_team', {}).get('name', '?')} vs {details.get('away_team', {}).get('name', '?')}")
                else:
                    self.stdout.write(self.style.ERROR("  ❌ Not found"))
                return
            
            success = 0
            try:
                if import_full_match(match_id, season_id, tournament_code=tournament_code):
                    success += 1
                    self.stdout.write(self.style.SUCCESS(f"  ✅ Match {match_id}"))
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"  ❌ {match_id}: {e}"))
            self.stdout.write(self.style.SUCCESS(f"\nDone: {success}/1"))
            return
        
        # === Список матчей ===
        if options['match_ids']:
            match_ids = [int(x.strip()) for x in options['match_ids'].split(',') if x.strip().isdigit()]
            season_id = options['season']
            self.stdout.write(f"Importing {len(match_ids)} matches (tournament={tournament_code})...")
            
            if dry_run:
                self.stdout.write(f"Would import: {match_ids[:10]}{'...' if len(match_ids) > 10 else ''}")
                return
            
            success = 0
            for mid in match_ids:
                try:
                    if import_full_match(mid, season_id, tournament_code=tournament_code):
                        success += 1
                except Exception as e:
                    self.stdout.write(self.style.ERROR(f"Error {mid}: {e}"))
            self.stdout.write(self.style.SUCCESS(f"Done: {success}/{len(match_ids)}"))
            return
        
        # === Сезон (с авто-определением) ===
        season_id = options['season']
        year = options['year']
        auto_detect = not no_auto_detect
        
        if season_id is None and auto_detect:
            client = KFFClient()
            
            # 🔥 DEBUG: Вывод сырых данных если включено
            if debug_api:
                self.stdout.write(self.style.WARNING(f"\n🔍 DEBUG: Fetching /seasons?tournament={tournament_code}..."))
                raw_resp = client._get("/seasons", params={"tournament": tournament_code})
                if raw_resp:
                    import json
                    sample = json.dumps(raw_resp, ensure_ascii=False, indent=2)[:2000]
                    self.stdout.write(f"Response preview:\n{sample}\n")
                else:
                    self.stdout.write(self.style.ERROR("Response is None"))
            
            # Авто-определение сезона
            if tournament_code == KFFClient.TARGET_TOURNAMENT:
                season_id = client.find_premier_league_season(year=year)
            else:
                seasons = client.get_tournament_seasons(tournament_code=tournament_code)
                current = [s for s in seasons if s.get('is_current')]
                season_id = current[0]['id'] if current else (seasons[0]['id'] if seasons else None)
            
            if not season_id:
                self.stdout.write(self.style.ERROR(
                    f"❌ Could not auto-detect season for tournament '{tournament_code}'.\n"
                    f"Try: python manage.py sync_kff --season 200 --tournament {tournament_code}\n"
                    f"Or check API availability."
                ))
                return
            
            tournament_name = KFFClient.TOURNAMENT_CODES.get(tournament_code, tournament_code)
            self.stdout.write(self.style.SUCCESS(f"✅ Auto-detected {tournament_name} season: {season_id}"))
        
        if not season_id:
            self.stdout.write(self.style.ERROR("❌ Season ID required when auto-detect disabled"))
            return
        
        # === Только последние ЗАВЕРШЁННЫЕ матчи ===
        if options['recent_only'] and not options['full']:
            client = KFFClient()
            limit = options['limit']
            tournament_name = KFFClient.TOURNAMENT_CODES.get(tournament_code, tournament_code)
            self.stdout.write(f"Syncing recent {limit} FINISHED matches for {tournament_name} season {season_id}...")
            
            if dry_run:
                # ✅ Получаем последние завершённые матчи с датами
                recent_ids = client.get_recent_finished_matches(
                    season_id=season_id,
                    limit=limit,
                    tournament_code=tournament_code
                )
                self.stdout.write(f"Would import recent finished matches: {recent_ids}")
                return
            
            # ✅ Получаем последние завершённые матчи (с сортировкой по дате)
            recent_ids = client.get_recent_finished_matches(
                season_id=season_id,
                limit=limit,
                tournament_code=tournament_code
            )
            
            if not recent_ids:
                self.stdout.write(self.style.WARNING("⚠️  No finished matches found"))
                return
            
            self.stdout.write(f"Syncing {len(recent_ids)} recent finished matches...")
            
            success = 0
            for mid in recent_ids:
                try:
                    if import_full_match(mid, season_id, tournament_code=tournament_code):
                        success += 1
                except Exception as e:
                    self.stdout.write(self.style.ERROR(f"Error {mid}: {e}"))
            
            self.stdout.write(self.style.SUCCESS(f"Done: {success}/{len(recent_ids)} recent finished matches"))
            return
        
        # === Полная синхронизация ===
        tournament_name = KFFClient.TOURNAMENT_CODES.get(tournament_code, tournament_code)
        self.stdout.write(f"Syncing {tournament_name} season {season_id} (full)...")
        
        if dry_run:
            client = KFFClient()
            match_ids = client.get_season_matches(season_id=season_id, tournament_code=tournament_code, auto_detect=False)
            self.stdout.write(f"Would import {len(match_ids)} matches: {match_ids[:10]}{'...' if len(match_ids) > 10 else ''}")
            return
        
        results = sync_season(season_id=season_id, tournament_code=tournament_code, auto_detect=False)
        
        self.stdout.write("\n" + "="*50)
        self.stdout.write(f"Tournament: {tournament_name} ({tournament_code})")
        self.stdout.write(f"Total: {results['total']}")
        self.stdout.write(self.style.SUCCESS(f"Success: {results['success']}"))
        if results.get('failed'):
            self.stdout.write(self.style.ERROR(f"Failed: {results['failed']}"))
        if results.get('error'):
            self.stdout.write(self.style.ERROR(f"Error: {results['error']}"))
        self.stdout.write("="*50)