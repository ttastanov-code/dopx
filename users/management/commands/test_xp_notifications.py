# users/management/commands/test_xp_notifications.py
from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from django.utils import timezone
from users.models import UserXP, UserBadge
from notifications.models import Notification
import logging

User = get_user_model()
logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = 'Тестирование системы XP и уведомлений'
    
    def add_arguments(self, parser):
        parser.add_argument('--username', type=str, help='Имя пользователя для теста')
        parser.add_argument('--xp-amount', type=int, default=10, help='Количество XP для теста')
        parser.add_argument('--create-badge', action='store_true', help='Создать тестовое достижение')
        parser.add_argument('--send-notification', action='store_true', help='Отправить тестовое уведомление')
    
    def handle(self, *args, **options):
        username = options.get('username')
        xp_amount = options.get('xp_amount')
        create_badge = options.get('create_badge')
        send_notification = options.get('send_notification')
        
        self.stdout.write('\n' + '=' * 80)
        self.stdout.write('🧪 ТЕСТ XP И УВЕДОМЛЕНИЙ')
        self.stdout.write('=' * 80 + '\n')
        
        # Получаем пользователя
        if username:
            try:
                user = User.objects.get(username=username)
            except User.DoesNotExist:
                self.stdout.write(self.style.ERROR(f'❌ Пользователь {username} не найден'))
                return
        else:
            user = User.objects.filter(is_verified=True).first()
            if not user:
                self.stdout.write(self.style.ERROR('❌ Нет верифицированных пользователей'))
                return
        
        self.stdout.write(f'👤 Тестируем на пользователе: {user.username}')
        self.stdout.write(f'   Email: {user.email}')
        self.stdout.write(f'   Текущий XP: {getattr(user, "xp", None).total_xp if hasattr(user, "xp") else 0}')
        self.stdout.write(f'   Уровень: {getattr(user, "xp", None).level if hasattr(user, "xp") else 1}\n')
        
        # === ТЕСТ 1: Проверка UserXP ===
        self.stdout.write('📍 ТЕСТ 1: Проверка UserXP')
        xp, created = UserXP.objects.get_or_create(user=user)
        if created:
            self.stdout.write(self.style.WARNING(f'   ⚠️  UserXP создан заново (был удалён?)'))
        else:
            self.stdout.write(self.style.SUCCESS('   ✅ UserXP существует'))
        self.stdout.write(f'   XP до: {xp.total_xp} | Уровень: {xp.level}')
        
        # === ТЕСТ 2: Начисление XP ===
        self.stdout.write(f'\n📍 ТЕСТ 2: Начисление {xp_amount} XP')
        old_xp = xp.total_xp
        old_level = xp.level
        xp.add_xp(xp_amount)
        xp.refresh_from_db()
        
        if xp.total_xp == old_xp + xp_amount:
            self.stdout.write(self.style.SUCCESS(f'   ✅ XP начислен: {old_xp} → {xp.total_xp}'))
        else:
            self.stdout.write(self.style.ERROR(f'   ❌ XP НЕ начислен: {old_xp} → {xp.total_xp}'))
        
        if xp.level > old_level:
            self.stdout.write(self.style.SUCCESS(f'   ✅ Уровень повышен: {old_level} → {xp.level}'))
        
        # === ТЕСТ 3: Создание достижения ===
        if create_badge:
            self.stdout.write(f'\n📍 ТЕСТ 3: Создание достижения')
            badge, created = UserBadge.objects.get_or_create(
                user=user,
                badge_type='first_evaluation'
            )
            if created:
                self.stdout.write(self.style.SUCCESS('   ✅ Достижение создано'))
            else:
                self.stdout.write(self.style.WARNING(f'   ⚠️  Достижение уже существовало'))
            
            # === ТЕСТ 4: Создание уведомления о достижении ===
            self.stdout.write(f'\n📍 ТЕСТ 4: Создание уведомления о достижении')
            notification = Notification.objects.create(
                user=user,
                notification_type='new_badge',
                title='🎖️ Новое достижение!',
                message='Вы получили достижение "Первая оценка"',
                action_url='/users/profile/',
                is_read=False
            )
            self.stdout.write(self.style.SUCCESS(f'   ✅ Уведомление создано (ID: {str(notification.id)[:8]})'))
        
        # === ТЕСТ 5: Проверка уведомлений ===
        self.stdout.write(f'\n📍 ТЕСТ 5: Проверка уведомлений пользователя')
        total_notifications = Notification.objects.filter(user=user).count()
        unread_notifications = Notification.objects.filter(user=user, is_read=False).count()
        self.stdout.write(f'   Всего уведомлений: {total_notifications}')
        self.stdout.write(f'   Непрочитанных: {unread_notifications}')
        
        if total_notifications > 0:
            self.stdout.write(f'\n   Последние 5 уведомлений:')
            for notif in Notification.objects.filter(user=user).order_by('-created_at')[:5]:
                status = '🔴' if not notif.is_read else '🟢'
                self.stdout.write(f'   {status} [{notif.notification_type}] {notif.title[:50]}')
        
        # === ТЕСТ 6: Настройки уведомлений ===
        self.stdout.write(f'\n📍 ТЕСТ 6: Настройки уведомлений')
        notif_settings = user.notification_settings or {}
        self.stdout.write(f'   email_match_finished: {notif_settings.get("email_match_finished", True)}')
        self.stdout.write(f'   email_voting_open: {notif_settings.get("email_voting_open", True)}')
        self.stdout.write(f'   email_top_performance: {notif_settings.get("email_top_performance", True)}')
        self.stdout.write(f'   email_system: {notif_settings.get("email_system", True)}')
        
        # === ТЕСТ 7: Проверка Celery ===
        self.stdout.write(f'\n📍 ТЕСТ 7: Проверка Celery')
        from django.core.cache import cache
        from celery import current_app
        try:
            inspect = current_app.control.inspect()
            active = inspect.active()
            if active:
                self.stdout.write(self.style.SUCCESS('   ✅ Celery воркеры активны'))
            else:
                self.stdout.write(self.style.WARNING('   ⚠️  Celery воркеры не отвечают'))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'   ❌ Ошибка подключения к Celery: {e}'))
        
        # === ТЕСТ 8: Email конфигурация ===
        self.stdout.write(f'\n📍 ТЕСТ 8: Email конфигурация')
        from django.conf import settings
        self.stdout.write(f'   EMAIL_HOST: {settings.EMAIL_HOST}')
        self.stdout.write(f'   EMAIL_PORT: {settings.EMAIL_PORT}')
        self.stdout.write(f'   EMAIL_USE_TLS: {settings.EMAIL_USE_TLS}')
        self.stdout.write(f'   DEFAULT_FROM_EMAIL: {settings.DEFAULT_FROM_EMAIL}')
        
        if settings.EMAIL_HOST_USER:
            self.stdout.write(self.style.SUCCESS('   ✅ EMAIL_HOST_USER настроен'))
        else:
            self.stdout.write(self.style.WARNING('   ⚠️  EMAIL_HOST_USER не настроен'))
        
        # === ТЕСТ 9: Отправка тестового email ===
        if send_notification:
            self.stdout.write(f'\n📍 ТЕСТ 9: Отправка тестового email')
            try:
                from django.core.mail import send_mail
                from django.template.loader import render_to_string
                
                html_message = render_to_string('emails/notification_digest.html', {
                    'user': user,
                    'notifications': Notification.objects.filter(user=user)[:3],
                    'count': 1,
                    'site_name': 'DOPX',
                    'site_url': getattr(settings, 'SITE_URL', 'http://127.0.0.1:8000'),
                })
                
                send_mail(
                    subject='🧪 Тестовое уведомление DOPX',
                    message='Это тестовое уведомление от системы DOPX',
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    recipient_list=[user.email],
                    html_message=html_message,
                    fail_silently=False,
                )
                self.stdout.write(self.style.SUCCESS(f'   ✅ Email отправлен на {user.email}'))
            except Exception as e:
                self.stdout.write(self.style.ERROR(f'   ❌ Ошибка отправки email: {e}'))
        
        # === ИТОГИ ===
        self.stdout.write('\n' + '=' * 80)
        self.stdout.write('📊 ИТОГИ ТЕСТА')
        self.stdout.write('=' * 80)
        self.stdout.write(f'Пользователь: {user.username}')
        self.stdout.write(f'XP: {xp.total_xp} (Уровень {xp.level})')
        self.stdout.write(f'Достижений: {UserBadge.objects.filter(user=user).count()}')
        self.stdout.write(f'Уведомлений: {total_notifications} ({unread_notifications} непрочитанных)')
        self.stdout.write('=' * 80 + '\n')