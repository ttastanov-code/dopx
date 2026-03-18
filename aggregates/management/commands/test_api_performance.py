# aggregates/management/commands/test_api_performance.py
from django.core.management.base import BaseCommand
from django.test import Client
from django.contrib.auth import get_user_model
import time
import statistics

User = get_user_model()

class Command(BaseCommand):
    help = 'Тестирование производительности API'
    
    def handle(self, *args, **options):
        client = Client()
        
        # Создаём тестового пользователя
        user, _ = User.objects.get_or_create(
            username='perf_test_user',
            defaults={'email': 'perf@dopx.kz'}
        )
        user.set_password('testpass123')
        user.save()
        
        # Логинимся
        client.login(username='perf_test_user', password='testpass123')
        
        endpoints = [
            '/api/player-aggregate/',
            '/api/player-aggregate/top_players/?limit=10',
            '/api/match-aggregate/',
        ]
        
        results = {}
        
        for endpoint in endpoints:
            self.stdout.write(f'Тестирование {endpoint}...')
            times = []
            
            for i in range(10):
                start = time.time()
                response = client.get(endpoint)
                end = time.time()
                
                times.append((end - start) * 1000)  # ms
                
                if response.status_code != 200:
                    self.stdout.write(self.style.ERROR(f'  ❌ Status: {response.status_code}'))
                    break
            
            if len(times) == 10:
                avg = statistics.mean(times)
                p95 = sorted(times)[9]
                results[endpoint] = {'avg': avg, 'p95': p95}
                
                status = '✅' if avg < 800 else '⚠️'
                self.stdout.write(f'  {status} Avg: {avg:.2f}ms, P95: {p95:.2f}ms')
            else:
                self.stdout.write(self.style.ERROR(f'  ❌ Тест не завершён'))
        
        # Итоги
        self.stdout.write('\n' + '=' * 60)
        self.stdout.write('📊 РЕЗУЛЬТАТЫ ТЕСТИРОВАНИЯ')
        self.stdout.write('=' * 60)
        
        all_pass = True
        for endpoint, data in results.items():
            status = '✅ PASS' if data['avg'] < 800 else '⚠️  SLOW'
            if data['avg'] >= 800:
                all_pass = False
            self.stdout.write(f'{endpoint}: {status} ({data["avg"]:.2f}ms)')
        
        if all_pass:
            self.stdout.write(self.style.SUCCESS('\n✅ Все эндпоинты < 800ms'))
        else:
            self.stdout.write(self.style.WARNING('\n⚠️  Некоторые эндпоинты медленные'))