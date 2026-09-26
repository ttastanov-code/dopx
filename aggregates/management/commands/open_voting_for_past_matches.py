# aggregates/management/commands/open_voting_for_past_matches.py
# Открывает голосование для завершённых матчей (выставляет voting_open_until).
from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import timedelta
from matches.models import Match
import logging
from core.management.seed_guard import ensure_seed_allowed

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = 'Открыть голосование для всех завершённых матчей'
    
    def add_arguments(self, parser):
        parser.add_argument(
            '--hours',
            type=int,
            default=48,
            help='На сколько часов открыть голосование (по умолчанию 48)'
        )
        parser.add_argument(
            '--all',
            action='store_true',
            help='Открыть для ВСЕХ завершённых матчей'
        )
        parser.add_argument(
            '--days',
            type=int,
            default=7,
            help='За сколько дней брать матчи (по умолчанию 7)'
        )
    
    def handle(self, *args, **options):
        ensure_seed_allowed()
        hours = options['hours']
        all_matches = options['all']
        days = options['days']
        
        now = timezone.now()
        voting_until = now + timedelta(hours=hours)
        
        if all_matches:
            matches = Match.objects.filter(
                status='finished',
                voting_open_until__lt=now
            )
        else:
            matches = Match.objects.filter(
                status='finished',
                start_time__gte=now - timedelta(days=days),
                voting_open_until__lt=now
            )
        
        count = matches.count()
        self.stdout.write(f'📊 Найдено матчей для обновления: {count}')
        
        if count == 0:
            self.stdout.write(self.style.WARNING('⚠️  Матчей не найдено'))
            return
        
        updated = 0
        for match in matches:
            match.voting_open_until = voting_until
            match.save(update_fields=['voting_open_until', 'updated_at'])
            updated += 1
        
        self.stdout.write(self.style.SUCCESS(
            f'✅ Открыто голосование для {updated} матчей до {voting_until}'
        ))
        self.stdout.write(f'   Теперь можно тестировать оценки!')