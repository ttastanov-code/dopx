# aggregates/management/commands/recalculate_aggregates.py
from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import timedelta
from matches.models import Match
from aggregates.tasks import recalculate_all_aggregates_for_match
import logging

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = 'Пересчитать агрегаты для матчей'
    
    def add_arguments(self, parser):
        parser.add_argument(
            '--match-id',
            type=str,
            help='Пересчитать для конкретного матча (UUID)'
        )
        parser.add_argument(
            '--all-active',
            action='store_true',
            help='Пересчитать для всех активных матчей'
        )
        parser.add_argument(
            '--hours',
            type=int,
            default=24,
            help='Период в часах для поиска активных матчей (по умолчанию 24)'
        )
    
    def handle(self, *args, **options):
        match_id = options.get('match_id')
        all_active = options.get('all_active')
        hours = options.get('hours')
        
        if match_id:
            self.stdout.write(f"Recalculating aggregates for match {match_id}...")
            recalculate_all_aggregates_for_match.delay(match_id)
            self.stdout.write(self.style.SUCCESS("Task queued!"))
        elif all_active:
            now = timezone.now()
            active_matches = Match.objects.filter(
                voting_open_until__gte=now - timedelta(hours=hours)
            )
            
            self.stdout.write(f"Found {active_matches.count()} active matches")
            
            count = 0
            for match in active_matches:
                recalculate_all_aggregates_for_match.delay(str(match.id))
                count += 1
            
            self.stdout.write(self.style.SUCCESS(f"Queued {count} tasks!"))
        else:
            self.stdout.write(self.style.WARNING(
                "Specify --match-id or --all-active"
            ))