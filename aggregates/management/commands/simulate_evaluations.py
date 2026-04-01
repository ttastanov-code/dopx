# aggregates/management/commands/simulate_evaluations.py
from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from django.utils import timezone
from datetime import timedelta
import random
from uuid import UUID

from matches.models import Match
from players.models import Player
from teams.models import Team
from coaches.models import Coach
from evaluations.models import (
    ContextEvaluation,
    PlayerEvaluation,
    MatchEvaluation,
    TeamEvaluation,
    CoachEvaluation,
    RefereeEvaluation,
    EvaluationSession
)
from aggregates.tasks import recalculate_all_aggregates_for_match

User = get_user_model()

class Command(BaseCommand):
    help = 'Симулировать оценки от множества пользователей'
    
    def add_arguments(self, parser):
        parser.add_argument(
            '--users',
            type=int,
            default=50,
            help='Количество пользователей для симуляции'
        )
        parser.add_argument(
            '--matches',
            type=int,
            default=5,
            help='Количество матчей для оценки'
        )
        parser.add_argument(
            '--match-ids',
            type=str,
            help='Конкретные ID матчей (через запятую)'
        )
        parser.add_argument(
            '--recalculate',
            action='store_true',
            help='Пересчитать агрегаты после оценок'
        )
    
    def handle(self, *args, **options):
        users_count = options['users']
        matches_count = options['matches']
        match_ids = options.get('match_ids')
        recalculate = options['recalculate']
        
        # Получаем пользователей
        users = list(User.objects.filter(
            is_verified=True,
            username__startswith='test_user'
        )[:users_count])
        
        if len(users) < users_count:
            self.stdout.write(self.style.WARNING(
                f'⚠️  Найдено только {len(users)} тестовых пользователей'
            ))
        
        # Получаем матчи с открытым голосованием
        now = timezone.now()
        if match_ids:
            match_id_list = [mid.strip() for mid in match_ids.split(',')]
            matches = Match.objects.filter(
                id__in=match_id_list,
                voting_open_until__gte=now,
                status='finished'
            )
        else:
            matches = list(Match.objects.filter(
                voting_open_until__gte=now,
                status='finished'
            ).order_by('-start_time')[:matches_count])
        
        if not matches:
            self.stdout.write(self.style.ERROR(
                '❌ Нет матчей с открытым голосованием!'
            ))
            self.stdout.write('   Запустите: python manage.py open_voting_for_past_matches')
            return
        
        self.stdout.write(f'📊 Матчей для оценки: {len(matches)}')
        self.stdout.write(f'📊 Пользователей: {len(users)}')
        
        total_evaluations = 0
        
        for match in matches:
            self.stdout.write(f'\n📍 Матч: {match.home_team} vs {match.away_team}')
            
            # Получаем игроков матча
            from lineups.models import MatchLineupPlayer
            lineup_players = list(Player.objects.filter(
                matchlineupplayer__lineup__match=match
            ).distinct()[:11])
            
            match_evaluations = 0
            
            for user in users:
                try:
                    # 1. Context Evaluation
                    watched_types = ['full', 'highlights', 'partial']
                    context, _ = ContextEvaluation.objects.update_or_create(
                        user=user,
                        match=match,
                        defaults={
                            'watched_type': random.choice(watched_types),
                            'attended_stadium': random.random() < 0.2,
                            'supported_team': random.choice([
                                None,
                                match.home_team,
                                match.away_team
                            ]),
                        }
                    )
                    
                    # 2. Match Evaluation
                    MatchEvaluation.objects.update_or_create(
                        user=user,
                        match=match,
                        defaults={
                            'entertainment': random.randint(6, 10),
                            'tension': random.randint(5, 10),
                            'fairness': random.randint(6, 10),
                            'turning_point': random.random() < 0.3,
                        }
                    )
                    
                    # 3. Team Evaluations
                    for team in [match.home_team, match.away_team]:
                        TeamEvaluation.objects.update_or_create(
                            user=user,
                            match=match,
                            team=team,
                            defaults={
                                'tactics': random.randint(6, 10),
                                'effort': random.randint(6, 10),
                                'organization': random.randint(5, 10),
                                'mentality': random.randint(6, 10),
                            }
                        )
                    
                    # 4. Player Evaluations (оцениваем 5-7 игроков случайно)
                    players_to_eval = random.sample(
                        lineup_players,
                        min(len(lineup_players), random.randint(5, 7))
                    )
                    for player in players_to_eval:
                        PlayerEvaluation.objects.update_or_create(
                            user=user,
                            match=match,
                            player=player,
                            defaults={
                                'contribution': random.randint(5, 10),
                                'risk': random.randint(1, 5),
                                'potential': random.randint(6, 10),
                            }
                        )
                    
                    # 5. Coach Evaluations
                    for coach in [match.home_coach, match.away_coach]:
                        if coach:
                            CoachEvaluation.objects.update_or_create(
                                user=user,
                                match=match,
                                coach=coach,
                                defaults={
                                    'tactics': random.randint(6, 10),
                                    'substitutions': random.randint(5, 10),
                                    'game_management': random.randint(6, 10),
                                    'impact': random.randint(5, 10),
                                }
                            )
                    
                    # 6. Referee Evaluation
                    RefereeEvaluation.objects.update_or_create(
                        user=user,
                        match=match,
                        defaults={
                            'influence_score': random.randint(40, 90),
                            'decision_quality': random.randint(6, 10),
                        }
                    )
                    
                    # Обновляем сессию
                    EvaluationSession.objects.update_or_create(
                        user=user,
                        match=match,
                        defaults={
                            'status': 'completed',
                            'current_step': 'complete',
                            'completed_steps': ['context', 'teams', 'players', 
                                               'coaches', 'referee', 'match_eval'],
                            'completed_at': timezone.now(),
                        }
                    )
                    
                    match_evaluations += 1
                    total_evaluations += 1
                    
                except Exception as e:
                    self.stdout.write(self.style.ERROR(
                        f'   ❌ Ошибка для пользователя {user.username}: {e}'
                    ))
            
            self.stdout.write(f'   ✅ Оценок для матча: {match_evaluations}')
            
            # Пересчёт агрегатов
            if recalculate:
                self.stdout.write(f'   🔄 Пересчёт агрегатов...')
                recalculate_all_aggregates_for_match.delay(str(match.id))
        
        self.stdout.write(self.style.SUCCESS(
            f'\n🎉 Всего создано оценок: {total_evaluations}'
        ))