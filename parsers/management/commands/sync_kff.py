from django.core.management.base import BaseCommand
from parsers.kff.client import KFFClient
from parsers.kff.pipeline import sync_season, import_full_match
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Sync matches from KFF League API"
    
    def add_arguments(self, parser):
        parser.add_argument(
            '--season',
            type=int,
            default=200,
            help='Season ID to sync (default: 200)'
        )
        parser.add_argument(
            '--match-id',
            type=int,
            help='Sync a specific match ID only'
        )
        parser.add_argument(
            '--match-ids',
            type=str,
            help='Comma-separated list of match IDs to sync'
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be imported without saving'
        )
    
    def handle(self, *args, **options):
        dry_run = options['dry_run']
        
        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN MODE - No changes will be saved"))
        
        if options['match_id']:
            match_ids = [options['match_id']]
            season_id = None
        elif options['match_ids']:
            match_ids = [int(x.strip()) for x in options['match_ids'].split(',') if x.strip().isdigit()]
            season_id = None
        else:
            season_id = options['season']
            match_ids = None
        
        if dry_run:
            client = KFFClient()
            if not match_ids:
                match_ids = client.get_season_matches(season_id)
            self.stdout.write(f"Would import {len(match_ids)} matches: {match_ids[:10]}{'...' if len(match_ids) > 10 else ''}")
            return
        
        if match_ids:
            self.stdout.write(f"Importing {len(match_ids)} specific matches...")
            success = 0
            for mid in match_ids:
                try:
                    if import_full_match(mid, season_id):
                        success += 1
                except Exception as e:
                    self.stdout.write(self.style.ERROR(f"Error with match {mid}: {e}"))
            self.stdout.write(self.style.SUCCESS(
                f"Done: {success}/{len(match_ids)} matches imported"
            ))
        else:
            self.stdout.write(f"Syncing season {season_id}...")
            results = sync_season(season_id)
            
            self.stdout.write("\n" + "="*50)
            self.stdout.write(f"Total: {results['total']}")
            self.stdout.write(self.style.SUCCESS(f"Success: {results['success']}"))
            if results['failed']:
                self.stdout.write(self.style.ERROR(f"Failed: {results['failed']}"))
            self.stdout.write("="*50)