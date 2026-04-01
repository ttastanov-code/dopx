# users/management/commands/create_test_users.py
from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from django.utils import timezone
import random
import string

User = get_user_model()

class Command(BaseCommand):
    help = 'Создать тестовых пользователей для нагрузочного тестирования'
    
    def add_arguments(self, parser):
        parser.add_argument(
            '--count',
            type=int,
            default=100,
            help='Количество пользователей (по умолчанию 100)'
        )
        parser.add_argument(
            '--verified',
            action='store_true',
            help='Создать верифицированных пользователей'
        )
        parser.add_argument(
            '--prefix',
            type=str,
            default='test_user',
            help='Префикс для имён пользователей'
        )
    
    def handle(self, *args, **options):
        count = options['count']
        verified = options['verified']
        prefix = options['prefix']
        
        self.stdout.write(f'📊 Создание {count} тестовых пользователей...')
        
        created = 0
        for i in range(count):
            username = f'{prefix}_{i}_{timezone.now().strftime("%Y%m%d%H%M%S")}'
            email = f'{username}@test.dopx.kz'
            
            try:
                user = User.objects.create_user(
                    username=username,
                    email=email,
                    password='testpass123',
                    is_verified=verified,
                    trust_score=1.0 + random.uniform(0, 0.5),
                    city=random.choice(['Алматы', 'Астана', 'Шымкент', 'Караганда']),
                )
                
                # Создаём XP профиль
                from users.models import UserXP
                UserXP.objects.get_or_create(user=user)
                
                created += 1
                
                if created % 10 == 0:
                    self.stdout.write(f'   Создано: {created}/{count}')
                    
            except Exception as e:
                self.stdout.write(self.style.ERROR(f'   ❌ Ошибка для {username}: {e}'))
        
        self.stdout.write(self.style.SUCCESS(
            f'✅ Создано {created} пользователей'
        ))
        self.stdout.write(f'   Логин: {prefix}_X_*')
        self.stdout.write(f'   Пароль: testpass123')