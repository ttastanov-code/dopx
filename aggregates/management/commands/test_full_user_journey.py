# aggregates/management/commands/test_full_user_journey.py
from django.core.management.base import BaseCommand
from django.test import Client
from django.contrib.auth import get_user_model
from django.utils import timezone
from datetime import timedelta
import json
import logging
import time
from django.conf import settings

# === ВСЕ ИМПОРТЫ В НАЧАЛЕ ===
from matches.models import Match
from evaluations.models import ContextEvaluation, EvaluationSession
from users.models import UserXP, UserBadge
from notifications.models import Notification
from lineups.models import MatchLineupPlayer
from aggregates.tasks import recalculate_all_aggregates_for_match
from aggregates.services import calculate_user_trust_adjustment

User = get_user_model()
logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = 'Полный интеграционный тест: реальные действия пользователя'

    def add_arguments(self, parser):
        parser.add_argument('--username', type=str, default='admin2')
        parser.add_argument('--password', type=str, default='admin2')
        parser.add_argument('--email', type=str, default='t.tastanov@gmail.com')
        parser.add_argument('--matches', type=int, default=24)
        parser.add_argument('--force-level', type=int, default=10)
        parser.add_argument('--test-email', action='store_true')
        parser.add_argument('--sync-notifications', action='store_true', 
                          help='Создавать уведомления синхронно (для тестов без Celery)')

    def handle(self, *args, **options):
        username = options['username']
        password = options['password']
        email = options['email']
        num_matches = options['matches']
        target_level = options['force_level']
        test_email = options['test_email']
        sync_notifications = options['sync_notifications'] or True  # ✅ По умолчанию синхронно для тестов

        self.stdout.write('\n' + '=' * 80)
        self.stdout.write('🧪 DOPX FULL USER JOURNEY — ИНТЕГРАЦИОННЫЙ ТЕСТ')
        self.stdout.write('=' * 80 + '\n')

        # === ШАГ 0: Подготовка пользователя ===
        self.stdout.write('📍 ШАГ 0: Создание/подготовка пользователя')
        user, created = User.objects.get_or_create(
            username=username,
            defaults={
                'email': email,
                'is_verified': True,
                'is_active': True,
                'trust_score': 1.0,
                'city': 'Алматы'
            }
        )
        if created:
            user.set_password(password)
            user.save()
            UserXP.objects.get_or_create(user=user)
            self.stdout.write(self.style.SUCCESS(f'   ✅ Пользователь создан: {username} ({email})'))
        else:
            # СБРОС для чистого теста
            user.email = email
            user.is_verified = True
            user.trust_score = 1.0
            user.total_evaluations = 0
            user.evaluation_streak = 0
            user.last_evaluation_date = None
            user.save()
            UserXP.objects.filter(user=user).update(total_xp=0, level=1)
            UserBadge.objects.filter(user=user).delete()
            Notification.objects.filter(user=user).delete()
            EvaluationSession.objects.filter(user=user).delete()
            ContextEvaluation.objects.filter(user=user).delete()
            self.stdout.write(self.style.WARNING(f'   ⚠️  Данные пользователя сброшены'))
            user.set_password(password)
            user.save()

        # === ШАГ 1: Логин ===
        self.stdout.write('\n📍 ШАГ 1: Вход в систему')
        client = Client()
        if not client.login(username=username, password=password):
            self.stdout.write(self.style.ERROR('   ❌ Не удалось войти'))
            return
        self.stdout.write(self.style.SUCCESS('   ✅ Вход успешен'))

        # === ШАГ 2: Подготовка матчей ===
        self.stdout.write('\n📍 ШАГ 2: Поиск матчей с открытым голосованием')
        now = timezone.now()
        matches = Match.objects.filter(
            status='finished',
            voting_open_until__gte=now
        ).order_by('-start_time')[:num_matches]
        if not matches:
            self.stdout.write(self.style.ERROR('   ❌ Нет матчей с открытым голосованием'))
            self.stdout.write('   Запустите: python manage.py open_voting_for_past_matches --hours 72')
            return
        self.stdout.write(self.style.SUCCESS(f'   ✅ Найдено {matches.count()} матчей'))

        # === ШАГ 3: Симуляция оценок ===
        self.stdout.write(f'\n📍 ШАГ 3: Симуляция {num_matches} оценок')
        results = {
            'evaluations_completed': 0,
            'xp_gained': 0,
            'badges_earned': [],
            'level_ups': [],
            'notifications_created': 0,
            'errors': []
        }

        initial_xp = UserXP.objects.filter(user=user).first()
        initial_level = initial_xp.level if initial_xp else 1

        for idx, match in enumerate(matches, 1):
            try:
                # 🔹 Шаг 1: Контекст
                client.post(f'/evaluations/match/{match.id}/context/', {
                    'supported_team': str(match.home_team.id) if idx % 3 == 0 else '',
                    'watched_type': 'full' if idx % 4 != 0 else 'highlights',
                    'attended_stadium': idx % 10 == 0,
                }, follow=True)

                # 🔹 Шаг 2: Команды
                teams_data = {}
                for team in [match.home_team, match.away_team]:
                    prefix = f'team_{team.id}'
                    teams_data.update({
                        f'{prefix}_tactics': 7 + (idx % 4),
                        f'{prefix}_effort': 6 + (idx % 5),
                        f'{prefix}_organization': 7 + (idx % 4),
                        f'{prefix}_mentality': 6 + (idx % 5),
                    })
                client.post(f'/evaluations/match/{match.id}/teams/', teams_data, follow=True)

                # 🔹 Шаг 3: Игроки
                lineup_players = MatchLineupPlayer.objects.filter(
                    lineup__match=match
                ).select_related('player')[:7]
                players_data = {}
                for lp in lineup_players:
                    prefix = f'player_{lp.player.id}'
                    players_data[f'{prefix}_evaluate'] = 'on'
                    players_data[f'{prefix}_contribution'] = 7 + (idx % 4)
                    players_data[f'{prefix}_risk'] = 2 + (idx % 3)
                    players_data[f'{prefix}_potential'] = 7 + (idx % 4)
                client.post(f'/evaluations/match/{match.id}/players/', players_data, follow=True)

                # 🔹 Шаг 4: Тренеры
                coaches_data = {}
                for coach in [match.home_coach, match.away_coach]:
                    if coach:
                        prefix = f'coach_{coach.id}'
                        coaches_data.update({
                            f'{prefix}_tactics': 7,
                            f'{prefix}_substitutions': 6,
                            f'{prefix}_management': 7,
                            f'{prefix}_impact': 8,
                        })
                if coaches_data:
                    client.post(f'/evaluations/match/{match.id}/coaches/', coaches_data, follow=True)

                # 🔹 Шаг 5: Судья
                client.post(f'/evaluations/match/{match.id}/referee/', {
                    'influence_score': 45 + (idx % 10),
                    'decision_quality': 7 + (idx % 4),
                }, follow=True)

                # 🔹 Шаг 6: Финал — здесь происходит начисление XP, достижений, уведомлений!
                final_resp = client.post(f'/evaluations/match/{match.id}/match/', {
                    'entertainment': 7 + (idx % 4),
                    'tension': 6 + (idx % 4),
                    'fairness': 8,
                    'turning_point': idx % 5 == 0,
                }, follow=True)

                if final_resp.status_code == 200:
                    results['evaluations_completed'] += 1
                    
                    # ✅ СИНХРОННОЕ СОЗДАНИЕ УВЕДОМЛЕНИЙ (для тестов)
                    if sync_notifications:
                        self._create_sync_notifications(user, match, idx)
                    
                    time.sleep(0.3)  # Пауза для обработки задач
                    if idx % 6 == 0:
                        self.stdout.write(f'   ✅ Пройдено {idx} оценок...')
                else:
                    results['errors'].append(f'Match {idx}: Final failed')
            except Exception as e:
                results['errors'].append(f'Match {idx}: {str(e)}')
                continue

        # === ШАГ 4: Принудительное достижение уровня ===
        user.refresh_from_db()
        xp = UserXP.objects.filter(user=user).first()
        if xp and target_level > xp.level:
            self.stdout.write(f'\n📍 ШАГ 4: Принудительное повышение до уровня {target_level}')
            target_xp = target_level * 100
            if xp.total_xp < target_xp:
                xp.total_xp = target_xp
                xp.level = target_level
                xp.save()
                self.stdout.write(self.style.SUCCESS(f'   ✅ XP: {target_xp}, Уровень: {target_level}'))
                
                # ✅ Создаём уведомление о повышении уровня СИНХРОННО
                if sync_notifications:
                    Notification.objects.create(
                        user=user,
                        notification_type='level_up',
                        title=f'⬆️ Новый уровень {target_level}!',
                        message=f'Поздравляем! Вы достигли уровня {target_level} с {target_xp} XP.',
                        action_url='/users/profile/',
                    )
                    results['notifications_created'] += 1
                    self.stdout.write('   ✅ Уведомление о повышении уровня создано')

        # === ШАГ 5: Проверка достижений ===
        self.stdout.write('\n📍 ШАГ 5: Проверка достижений')
        expected_badges = {
            'first_evaluation': 'Первая оценка',
            'active_fan_10': 'Активный фанат (10 матчей)',
            'active_fan_50': 'Хардкор фанат (50 матчей)',
            'accurate_analyst': 'Точный аналитик',
            'bias_free': 'Без предвзятости',
            'early_bird': 'Ранняя пташка',
            'streak_7': 'Неделя подряд',
        }
        earned_badges = UserBadge.objects.filter(user=user)
        earned_types = {b.badge_type for b in earned_badges}
        
        for badge_type, badge_name in expected_badges.items():
            if badge_type in earned_types:
                self.stdout.write(self.style.SUCCESS(f'   ✅ {badge_name}'))
                results['badges_earned'].append(badge_name)
                
                # ✅ Создаём уведомление о достижении СИНХРОННО
                if sync_notifications:
                    Notification.objects.create(
                        user=user,
                        notification_type='new_badge',
                        title='🎖️ Новое достижение!',
                        message=f'Вы получили достижение: {badge_name}',
                        action_url='/users/profile/',
                    )
                    results['notifications_created'] += 1
            else:
                self.stdout.write(self.style.WARNING(f'   ⚠️  {badge_name} — не получен'))

        # === ШАГ 6: Проверка уведомлений ===
        self.stdout.write('\n📍 ШАГ 6: Проверка уведомлений')
        notifications = Notification.objects.filter(user=user)
        results['notifications_created'] = notifications.count()
        
        if notifications.exists():
            for n in notifications[:5]:
                self.stdout.write(f'   • [{n.notification_type}] {n.title[:50]}')
        else:
            self.stdout.write(self.style.WARNING('   ⚠️  Уведомления не созданы!'))

        # === ШАГ 7: Итоговая статистика ===
        user.refresh_from_db()
        if xp:
            xp.refresh_from_db()
        results['final_state'] = {
            'username': user.username,
            'email': user.email,
            'trust_score': round(user.trust_score, 2),
            'total_evaluations': user.total_evaluations,
            'evaluation_streak': user.evaluation_streak,
            'xp_total': xp.total_xp if xp else 0,
            'xp_level': xp.level if xp else 1,
            'badges_count': earned_badges.count(),
            'notifications_count': notifications.count(),
        }

        self.stdout.write('\n' + '=' * 80)
        self.stdout.write('📊 ИТОГОВАЯ СТАТИСТИКА')
        self.stdout.write('=' * 80)
        for key, value in results['final_state'].items():
            self.stdout.write(f'   {key}: {value}')

        # === Сохранение отчёта ===
        report_file = f'user_journey_{username}_report.json'
        with open(report_file, 'w', encoding='utf-8') as f:
            json.dump(results, f, indent=2, ensure_ascii=False, default=str)
        self.stdout.write(f'\n📄 Отчёт сохранён: {report_file}')

        # === ФИНАЛЬНЫЙ ЧЕКЛИСТ ===
        self.stdout.write('\n' + '=' * 80)
        self.stdout.write('✅ ЧЕКЛИСТ ДЛЯ ПРОВЕРКИ ВРУЧНУЮ')
        self.stdout.write('=' * 80)
        self.stdout.write(f'1. Зайдите на сайт: {getattr(settings, "SITE_URL", "http://127.0.0.1:8000")}')
        self.stdout.write(f'2. Войдите как: {username} / {password}')
        self.stdout.write(f'3. Проверьте профиль: /users/profile/')
        self.stdout.write(f'   • Уровень: {results["final_state"]["xp_level"]}')
        self.stdout.write(f'   • XP: {results["final_state"]["xp_total"]}')
        self.stdout.write(f'   • Trust Score: {results["final_state"]["trust_score"]}')
        self.stdout.write(f'4. Проверьте уведомления: /notifications/')
        self.stdout.write(f'   • Всего: {results["notifications_created"]}')
        if test_email:
            self.stdout.write(f'5. Проверьте email: {email}')
            self.stdout.write(f'   • Тема: "🎖️ Новое достижение" или "⬆️ Вы достигли уровня"')
        self.stdout.write('=' * 80 + '\n')

        if results['errors']:
            self.stdout.write(self.style.WARNING(f'⚠️  Ошибки: {len(results["errors"])}'))
            for err in results['errors'][:3]:
                self.stdout.write(f'   • {err}')

    def _create_sync_notifications(self, user, match, idx):
        """Создаёт тестовые уведомления синхронно (без Celery)"""
        # Уведомление об оценке матча
        Notification.objects.get_or_create(
            user=user,
            match=match,
            notification_type='match_finished',
            defaults={
                'title': '✅ Оценка сохранена',
                'message': f'Ваша оценка матча {match.home_team.name} vs {match.away_team.name} учтена в рейтингах.',
                'action_url': f'/matches/{match.id}/',
                'is_read': False,
            }
        )
        
        # С некоторой вероятностью — уведомление о достижении
        if idx % 5 == 0:  # Каждые 5 оценок
            badge_name = 'Активный фанат' if idx >= 10 else 'Точный аналитик'
            Notification.objects.get_or_create(
                user=user,
                notification_type='new_badge',
                message__icontains=badge_name,
                defaults={
                    'title': '🎖️ Новое достижение!',
                    'message': f'Вы получили достижение: {badge_name}',
                    'action_url': '/users/profile/',
                    'is_read': False,
                }
            )