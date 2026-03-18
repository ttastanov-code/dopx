# aggregates/management/commands/benchmark_full.py

from django.core.management.base import BaseCommand
from django.test import Client
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection
from django.utils import timezone
from datetime import timedelta
import time
import statistics
import json
from matches.models import Match
from evaluations.models import PlayerEvaluation, MatchEvaluation, ContextEvaluation

User = get_user_model()

class Command(BaseCommand):
    help = 'Полный бенчмарк производительности API'
    
    def handle(self, *args, **options):
        self.stdout.write('\n' + '=' * 80)
        self.stdout.write('🚀 DOPX FULL PERFORMANCE BENCHMARK')
        self.stdout.write('=' * 80 + '\n')
        
        # Создаём тестового пользователя
        user, _ = User.objects.get_or_create(
            username='benchmark_user',
            defaults={'email': 'benchmark@dopx.kz', 'is_verified': True}
        )
        user.set_password('testpass123')
        user.save()
        
        client = Client()
        client.login(username='benchmark_user', password='testpass123')
        
        # Тестовые эндпоинты
        endpoints = [
            {'url': '/api/player-aggregate/', 'name': 'Player Aggregates List', 'auth': True},
            {'url': '/api/player-aggregate/top_players/?limit=10', 'name': 'Top Players', 'auth': False},
            {'url': '/api/match-aggregate/', 'name': 'Match Aggregates List', 'auth': False},
            {'url': '/api/match-aggregate/recent/?limit=10', 'name': 'Recent Matches', 'auth': False},
            {'url': '/api/context/', 'name': 'Context Evaluations', 'auth': True},
            {'url': '/api/player/', 'name': 'Player Evaluations', 'auth': True},
        ]
        
        results = {}
        
        for endpoint_info in endpoints:
            endpoint = endpoint_info['url']
            name = endpoint_info['name']
            
            self.stdout.write(f'\n📍 Тестирование: {name}')
            self.stdout.write(f'   URL: {endpoint}')
            
            times = []
            queries_list = []
            
            for i in range(20):
                # Очищаем кэш каждые 5 итераций для честного теста
                if i % 5 == 0:
                    cache.clear()
                
                # Сбрасываем счетчик запросов
                connection.queries_log.clear()
                
                start = time.time()
                response = client.get(endpoint)
                end = time.time()
                
                duration_ms = (end - start) * 1000
                queries_count = len(connection.queries)
                
                times.append(duration_ms)
                queries_list.append(queries_count)
                
                if response.status_code != 200:
                    self.stdout.write(self.style.ERROR(f'   ❌ Status: {response.status_code}'))
                    break
            
            if len(times) == 20:
                avg_time = statistics.mean(times)
                p50 = statistics.median(times)
                p95 = sorted(times)[18]
                p99 = sorted(times)[19]
                min_time = min(times)
                max_time = max(times)
                
                avg_queries = statistics.mean(queries_list)
                
                results[endpoint] = {
                    'name': name,
                    'avg_ms': round(avg_time, 2),
                    'p50_ms': round(p50, 2),
                    'p95_ms': round(p95, 2),
                    'p99_ms': round(p99, 2),
                    'min_ms': round(min_time, 2),
                    'max_ms': round(max_time, 2),
                    'avg_queries': round(avg_queries, 1),
                    'status': 'PASS' if avg_time < 800 else 'FAIL'
                }
                
                # Оценка
                if avg_time < 100:
                    status = '✅ EXCELLENT'
                elif avg_time < 300:
                    status = '✅ GOOD'
                elif avg_time < 800:
                    status = '⚠️  ACCEPTABLE'
                else:
                    status = '❌ SLOW'
                
                self.stdout.write(f'   {status}')
                self.stdout.write(f'   ⏱️  Avg: {avg_time:.2f}ms | P50: {p50:.2f}ms | P95: {p95:.2f}ms')
                self.stdout.write(f'   📊 Queries: {avg_queries:.1f} avg')
        
        # Итоги
        self.stdout.write('\n' + '=' * 80)
        self.stdout.write('📊 ИТОГОВЫЕ РЕЗУЛЬТАТЫ')
        self.stdout.write('=' * 80)
        
        all_pass = True
        for endpoint, data in results.items():
            status = '✅ PASS' if data['status'] == 'PASS' else '❌ FAIL'
            if data['status'] == 'FAIL':
                all_pass = False
            self.stdout.write(f'{data["name"]}: {status} ({data["avg_ms"]}ms avg, {data["avg_queries"]} queries)')
        
        self.stdout.write('\n' + '=' * 80)
        if all_pass:
            self.stdout.write(self.style.SUCCESS('✅ ВСЕ ЭНДПОИНТЫ < 800ms — PERFORMANCE 100%!'))
        else:
            self.stdout.write(self.style.WARNING('⚠️  Некоторые эндпоинты требуют оптимизации'))
        self.stdout.write('=' * 80 + '\n')
        
        # Сохраняем отчёт
        report_data = {
            'timestamp': timezone.now().isoformat(),
            'results': results,
            'all_pass': all_pass
        }
        
        with open('benchmark_report.json', 'w', encoding='utf-8') as f:
            json.dump(report_data, f, indent=2, ensure_ascii=False)
        
        self.stdout.write('📄 Отчёт сохранён: benchmark_report.json\n')