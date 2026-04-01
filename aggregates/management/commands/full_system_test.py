# aggregates/management/commands/full_system_test.py
from django.core.management.base import BaseCommand
from django.test import Client
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection
from django.utils import timezone
from datetime import timedelta
import time
import json
import logging

from matches.models import Match
from evaluations.models import ContextEvaluation, MatchEvaluation, PlayerEvaluation
from aggregates.models import MatchAggregate, PlayerMatchAggregate

User = get_user_model()
logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = 'Полное системное тестирование всего функционала'
    
    def add_arguments(self, parser):
        parser.add_argument('--users', type=int, default=20)
        parser.add_argument('--matches', type=int, default=3)
        parser.add_argument('--output', type=str, default='test_report.json')
    
    def handle(self, *args, **options):
        self.stdout.write('\n' + '=' * 80)
        self.stdout.write('🚀 DOPX FULL SYSTEM TEST')
        self.stdout.write('=' * 80 + '\n')
        
        report = {
            'timestamp': timezone.now().isoformat(),
            'tests': {},
            'summary': {}
        }
        
        # === ТЕСТ 1: Проверка БД и кэша ===
        self.stdout.write('\n📍 ТЕСТ 1: Подключение к БД и кэшу')
        try:
            connection.ensure_connection()
            cache.set('test_key', 'test_value', 10)
            assert cache.get('test_key') == 'test_value'
            self.stdout.write(self.style.SUCCESS('   ✅ БД и кэш работают'))
            report['tests']['database_cache'] = 'PASS'
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'   ❌ Ошибка: {e}'))
            report['tests']['database_cache'] = 'FAIL'
        
        # === ТЕСТ 2: Создание тестовых пользователей ===
        self.stdout.write('\n📍 ТЕСТ 2: Создание пользователей')
        users_count = options['users']
        users = []
        for i in range(users_count):
            username = f'fulltest_user_{i}_{int(time.time())}'
            try:
                user = User.objects.create_user(
                    username=username,
                    email=f'{username}@test.dopx.kz',
                    password='testpass123',
                    is_verified=True,
                    trust_score=1.0 + (i * 0.05)
                )
                from users.models import UserXP
                UserXP.objects.get_or_create(user=user)
                users.append(user)
            except Exception as e:
                self.stdout.write(self.style.ERROR(f'   ❌ {username}: {e}'))
        
        self.stdout.write(self.style.SUCCESS(f'   ✅ Создано {len(users)} пользователей'))
        report['tests']['user_creation'] = f'{len(users)}/{users_count}'
        
        # === ТЕСТ 3: Открытие голосования ===
        self.stdout.write('\n📍 ТЕСТ 3: Открытие голосования для матчей')
        now = timezone.now()
        matches = list(Match.objects.filter(
            status='finished',
            voting_open_until__lt=now
        ).order_by('-start_time')[:options['matches']])
        
        for match in matches:
            match.voting_open_until = now + timedelta(hours=48)
            match.save(update_fields=['voting_open_until'])
        
        self.stdout.write(self.style.SUCCESS(f'   ✅ Открыто {len(matches)} матчей'))
        report['tests']['voting_open'] = len(matches)
        
        # === ТЕСТ 4: Симуляция оценок ===
        self.stdout.write('\n📍 ТЕСТ 4: Симуляция оценок')
        from aggregates.management.commands.simulate_evaluations import Command as SimulateCommand
        from io import StringIO
        
        total_evals = 0
        for match in matches[:2]:  # Тестируем на 2 матчах
            for user in users[:10]:  # 10 пользователей на матч
                try:
                    # Context
                    ContextEvaluation.objects.update_or_create(
                        user=user, match=match,
                        defaults={'watched_type': 'full'}
                    )
                    # Match
                    MatchEvaluation.objects.update_or_create(
                        user=user, match=match,
                        defaults={
                            'entertainment': 8,
                            'tension': 7,
                            'fairness': 8,
                        }
                    )
                    # Players
                    from players.models import Player
                    from lineups.models import MatchLineupPlayer
                    players = list(Player.objects.filter(
                        matchlineupplayer__lineup__match=match
                    )[:5])
                    for player in players:
                        PlayerEvaluation.objects.update_or_create(
                            user=user, match=match, player=player,
                            defaults={
                                'contribution': 7,
                                'risk': 3,
                                'potential': 8,
                            }
                        )
                    total_evals += 1
                except Exception as e:
                    self.stdout.write(self.style.ERROR(f'   ❌ {user.username}: {e}'))
        
        self.stdout.write(self.style.SUCCESS(f'   ✅ Создано {total_evals} оценок'))
        report['tests']['evaluations'] = total_evals
        
        # === ТЕСТ 5: Пересчёт агрегатов ===
        self.stdout.write('\n📍 ТЕСТ 5: Пересчёт агрегатов')
        from aggregates.tasks import recalculate_all_aggregates_for_match
        for match in matches[:2]:
            recalculate_all_aggregates_for_match.delay(str(match.id))
        time.sleep(3)  # Ждём выполнения задач
        
        aggregates_created = MatchAggregate.objects.filter(
            match__in=matches[:2]
        ).count()
        self.stdout.write(self.style.SUCCESS(f'   ✅ Создано {aggregates_created} агрегатов'))
        report['tests']['aggregates'] = aggregates_created
        
        # === ТЕСТ 6: API тестирование ===
        self.stdout.write('\n📍 ТЕСТ 6: Тестирование API')
        client = Client()
        if users:
            client.login(username=users[0].username, password='testpass123')
        
        api_tests = []
        endpoints = [
            ('/api/match-aggregate/', 'Match Aggregates'),
            ('/api/player-aggregate/', 'Player Aggregates'),
            ('/api/player-aggregate/top_players/?limit=5', 'Top Players'),
        ]
        
        for endpoint, name in endpoints:
            start = time.time()
            response = client.get(endpoint)
            duration = (time.time() - start) * 1000
            status = '✅' if response.status_code == 200 else '❌'
            self.stdout.write(f'   {status} {name}: {duration:.0f}ms ({response.status_code})')
            api_tests.append({
                'endpoint': endpoint,
                'name': name,
                'status': response.status_code,
                'duration_ms': round(duration, 2)
            })
        
        report['tests']['api'] = api_tests
        
        # === ТЕСТ 7: Проверка уведомлений ===
        self.stdout.write('\n📍 ТЕСТ 7: Проверка уведомлений')
        from notifications.models import Notification
        notif_count = Notification.objects.count()
        self.stdout.write(self.style.SUCCESS(f'   ✅ В системе {notif_count} уведомлений'))
        report['tests']['notifications'] = notif_count
        
        # === ИТОГИ ===
        self.stdout.write('\n' + '=' * 80)
        self.stdout.write('📊 ИТОГОВЫЙ ОТЧЁТ')
        self.stdout.write('=' * 80)
        
        passed = sum(1 for v in report['tests'].values() 
                    if v == 'PASS' or (isinstance(v, int) and v > 0))
        total = len(report['tests'])
        
        report['summary'] = {
            'total_tests': total,
            'passed': passed,
            'success_rate': f'{(passed/total)*100:.1f}%'
        }
        
        self.stdout.write(f'Пройдено тестов: {passed}/{total}')
        self.stdout.write(f'Успешность: {(passed/total)*100:.1f}%')
        
        # Сохранение отчёта
        with open(options['output'], 'w', encoding='utf-8') as f:
            json.dump(report, f, indent=2, ensure_ascii=False, default=str)
        
        self.stdout.write(self.style.SUCCESS(f'\n📄 Отчёт сохранён: {options["output"]}'))
        self.stdout.write('=' * 80 + '\n')