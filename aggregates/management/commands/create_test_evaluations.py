# aggregates/management/commands/create_test_evaluations.py
from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from django.utils import timezone
from datetime import timedelta
import random
import uuid
from matches.models import Match
from players.models import Player
from teams.models import Team
from evaluations.models import (
    ContextEvaluation,
    PlayerEvaluation,
    MatchEvaluation,
    CoachEvaluation,
    TeamEvaluation,
    RefereeEvaluation
)

User = get_user_model()

class Command(BaseCommand):
    help = 'Создать тестовые оценки для матча'

    def add_arguments(self, parser):
        parser.add_argument('--match-id', type=str, required=True, help='UUID матча')
        parser.add_argument('--users', type=int, default=5, help='Количество тестовых пользователей')

    def handle(self, *args, **options):
        match_id = options['match_id']
        num_users = options['users']
        
        # 🔧 Поиск по UUID или external_id
        match = None
        try:
            # Сначала пробуем как UUID
            from uuid import UUID
            match = Match.objects.get(id=UUID(match_id))
        except (ValueError, Match.DoesNotExist):
            # Если не получилось — ищем по external_id
            try:
                match = Match.objects.get(external_id=match_id)
            except Match.DoesNotExist:
                self.stdout.write(self.style.ERROR(f"Матч {match_id} не найден"))
                return

        self.stdout.write(f"Матч: {match.home_team} vs {match.away_team}")
        self.stdout.write(f"Статус: {match.status}")
        self.stdout.write(f"Счёт: {match.home_score}:{match.away_score}")

        # Создаём тестовых пользователей
        self.stdout.write(f"Создание {num_users} тестовых пользователей...")
        test_users = []
        for i in range(num_users):
            username = f"test_user_{match_id[:8]}_{i}"
            user, created = User.objects.get_or_create(
                username=username,
                defaults={
                    'email': f'{username}@test.com',
                    'trust_score': 1.0 + (i * 0.1),  # Разный trust_score
                    'is_verified': True,
                }
            )
            test_users.append(user)
            if created:
                user.set_password('testpass123')
                user.save()
        self.stdout.write(self.style.SUCCESS(f"Пользователи созданы: {len(test_users)}"))

        # Получаем игроков матча из lineup
        players = Player.objects.filter(
            matchlineupplayer__lineup__match=match
        ).distinct()
        self.stdout.write(f"Игроков для оценки: {players.count()}")

        # Создаём оценки для каждого пользователя
        for user in test_users:
            # ✅ FIX: Преобразуем UUID в int для модуля
            user_hash = user.id.int % 100
            
            # Context
            watched_types = ['full', 'highlights', 'partial']
            context, _ = ContextEvaluation.objects.update_or_create(
                user=user,
                match=match,
                defaults={
                    'watched_type': watched_types[user_hash % 3],
                    'attended_stadium': user_hash % 4 == 0,
                }
            )

            # Match Evaluation
            MatchEvaluation.objects.update_or_create(
                user=user,
                match=match,
                defaults={
                    'entertainment': random.randint(6, 10),
                    'tension': random.randint(5, 10),
                    'fairness': random.randint(6, 10),
                    'turning_point': user_hash % 5 == 0,
                }
            )

            # 🆕 Team Evaluations (для обеих команд)
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

            # 🆕 Referee Evaluation
            RefereeEvaluation.objects.update_or_create(
                user=user,
                match=match,
                defaults={
                    'influence_score': random.randint(40, 90),  # 0-100
                    'decision_quality': random.randint(6, 10),   # 1-10
                }
            )

            # Player Evaluations
            for player in players:
                contribution = random.randint(5, 10)
                risk = random.randint(1, 5)
                potential = random.randint(6, 10)
                
                PlayerEvaluation.objects.update_or_create(
                    user=user,
                    match=match,
                    player=player,
                    defaults={
                        'contribution': contribution,
                        'risk': risk,
                        'potential': potential,
                    }
                )

            # Coach Evaluations (если есть тренеры)
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

        self.stdout.write(self.style.SUCCESS("✅ Тестовые оценки созданы!"))
        self.stdout.write("Теперь запустите пересчёт агрегатов:")
        self.stdout.write(f"  python manage.py recalculate_aggregates --match-id {match_id}")