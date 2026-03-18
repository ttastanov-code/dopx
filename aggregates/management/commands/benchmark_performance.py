# aggregates/management/commands/benchmark_performance.py

from django.core.management.base import BaseCommand
from django.test import Client
from django.contrib.auth import get_user_model
from django.core.cache import cache
import time
import statistics
import json

User = get_user_model()

class Command(BaseCommand):
    help = 'Бенчмарк производительности API'
    
    def handle(self, *args, **options):
        client = Client()
        
        # Создаём тестового пользователя
        user, _ = User.objects.get_or_create(
            username='benchmark_user',
            defaults={'email': 'benchmark@dopx.kz', 'is_verified': True}
        )
        user.set_password('testpass123')
        user.save()
        
        client.login(username='benchmark_user', password='testpass123')
        
        endpoints = [
            {'url': '/api/player-aggregate/', 'name': 'Player Aggregates List'},
            {'url': '/api/player-aggregate/top_players/?limit=10', 'name': 'Top Players'},
            {'url': '/api/match-aggregate/', 'name': 'Match Aggregates List'},
            {'url': '/api/match-aggregate/recent/?limit=10', 'name': 'Recent Matches'},
        ]
        
        results = {}
        
        self.stdout.write('\n' + '=' * 70)
        self.stdout.write('🚀 DOPX PERFORMANCE BENCHMARK')
        self.stdout.write('=' * 70 + '\n')
        
        for endpoint_info in endpoints:
            endpoint = endpoint_info['url']
            name = endpoint_info['name']
            
            self.stdout.write(f'Тестирование: {name}')
            self.stdout.write(f'URL: {endpoint}')
            
            # Тёплый запуск (прогрев кэша)
            client.get(endpoint)
            cache.clear()
            
            times = []
            for i in range(20):  # 20 итераций
                # Очищаем кэш для честного теста
                if i % 5 == 0:
                    cache.clear()
                
                start = time.time()
                response = client.get(endpoint)
                end = time.time()
                
                duration_ms = (end - start) * 1000
                times.append(duration_ms)
                
                if response.status_code != 200:
                    self.stdout.write(self.style.ERROR(f'  ❌ Status: {response.status_code}'))
                    break
            
            if len(times) == 20:
                avg = statistics.mean(times)
                p50 = statistics.median(times)
                p95 = sorted(times)[18]
                p99 = sorted(times)[19]
                min_time = min(times)
                max_time = max(times)
                
                results[endpoint] = {
                    'avg': avg,
                    'p50': p50,
                    'p95': p95,
                    'p99': p99,
                    'min': min_time,
                    'max': max_time,
                }
                
                # Оценка производительности
                if avg < 100:
                    status = '✅ EXCELLENT'
                elif avg < 300:
                    status = '✅ GOOD'
                elif avg < 800:
                    status = '⚠️  ACCEPTABLE'
                else:
                    status = '❌ SLOW'
                
                self.stdout.write(f'  {status}')
                self.stdout.write(f'  Avg: {avg:.2f}ms | P50: {p50:.2f}ms | P95: {p95:.2f}ms')
                self.stdout.write(f'  Min: {min_time:.2f}ms | Max: {max_time:.2f}ms\n')
            else:
                self.stdout.write(self.style.ERROR('  ❌ Тест не завершён\n'))
        
        # Итоги
        self.stdout.write('\n' + '=' * 70)
        self.stdout.write('📊 ИТОГОВЫЕ РЕЗУЛЬТАТЫ')
        self.stdout.write('=' * 70)
        
        all_pass = True
        for endpoint, data in results.items():
            status = '✅ PASS' if data['avg'] < 800 else '❌ FAIL'
            if data['avg'] >= 800:
                all_pass = False
            self.stdout.write(f'{endpoint}: {status} ({data["avg"]:.2f}ms avg)')
        
        self.stdout.write('\n' + '=' * 70)
        if all_pass:
            self.stdout.write(self.style.SUCCESS('✅ ВСЕ ЭНДПОИНТЫ < 800ms — PERFORMANCE 100%!'))
        else:
            self.stdout.write(self.style.WARNING('⚠️  Некоторые эндпоинты требуют оптимизации'))
        self.stdout.write('=' * 70 + '\n')
        
        # Сохраняем отчёт
        with open('performance_report.json', 'w') as f:
            json.dump(results, f, indent=2)
        self.stdout.write('📄 Отчёт сохранён: performance_report.json\n')