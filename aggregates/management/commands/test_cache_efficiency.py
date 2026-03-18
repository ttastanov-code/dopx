# aggregates/management/commands/test_cache_efficiency.py

from django.core.management.base import BaseCommand
from django.core.cache import cache
from django.test import Client
from django.contrib.auth import get_user_model
import time

User = get_user_model()

class Command(BaseCommand):
    help = 'Тест эффективности кэширования'
    
    def handle(self, *args, **options):
        self.stdout.write('\n' + '=' * 60)
        self.stdout.write('📦 CACHE EFFICIENCY TEST')
        self.stdout.write('=' * 60 + '\n')
        
        client = Client()
        user, _ = User.objects.get_or_create(
            username='cache_test_user',
            defaults={'email': 'cache@dopx.kz', 'is_verified': True}
        )
        user.set_password('testpass123')
        user.save()
        client.login(username='cache_test_user', password='testpass123')
        
        endpoints = [
            '/api/player-aggregate/top_players/?limit=10',
            '/api/match-aggregate/recent/?limit=10',
        ]
        
        for endpoint in endpoints:
            self.stdout.write(f'\n📍 Endpoint: {endpoint}')
            
            # Первый запрос (cache miss)
            cache.clear()
            start = time.time()
            client.get(endpoint)
            miss_time = (time.time() - start) * 1000
            
            # Второй запрос (cache hit)
            start = time.time()
            client.get(endpoint)
            hit_time = (time.time() - start) * 1000
            
            # Третий запрос (cache hit)
            start = time.time()
            client.get(endpoint)
            hit_time2 = (time.time() - start) * 1000
            
            speedup = miss_time / hit_time if hit_time > 0 else 0
            
            self.stdout.write(f'   Cache MISS: {miss_time:.2f}ms')
            self.stdout.write(f'   Cache HIT:  {hit_time:.2f}ms')
            self.stdout.write(f'   Cache HIT:  {hit_time2:.2f}ms')
            self.stdout.write(f'   ⚡ Speedup: {speedup:.1f}x')
            
            if speedup >= 5:
                self.stdout.write(self.style.SUCCESS('   ✅ Кэш работает отлично!'))
            elif speedup >= 2:
                self.stdout.write(self.style.WARNING('   ⚠️  Кэш работает нормально'))
            else:
                self.stdout.write(self.style.ERROR('   ❌ Кэш не эффективен'))
        
        self.stdout.write('\n' + '=' * 60 + '\n')