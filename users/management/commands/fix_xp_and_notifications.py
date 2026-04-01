# users/management/commands/fix_xp_and_notifications.py
from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from users.models import UserXP, UserBadge
from notifications.models import Notification
from evaluations.models import ContextEvaluation
from django.db.models import Count

User = get_user_model()

class Command(BaseCommand):
    help = 'Исправить XP и уведомления для существующих пользователей'
    
    def handle(self, *args, **options):
        self.stdout.write('\n🔧 Исправление XP и уведомлений...\n')
        
        # === 1. Создаём UserXP для всех пользователей ===
        users_without_xp = User.objects.filter(xp__isnull=True)
        created_count = 0
        for user in users_without_xp:
            UserXP.objects.get_or_create(user=user)
            created_count += 1
        
        self.stdout.write(self.style.SUCCESS(f'✅ Создано {created_count} UserXP профилей'))
        
        # === 2. Пересчитываем XP на основе оценок ===
        self.stdout.write('\n📊 Пересчёт XP на основе оценок...')
        for user in User.objects.all():
            xp, _ = UserXP.objects.get_or_create(user=user)
            evaluation_count = ContextEvaluation.objects.filter(user=user).count()
            expected_xp = evaluation_count * 10
            
            if xp.total_xp < expected_xp:
                old_xp = xp.total_xp
                xp.total_xp = expected_xp
                xp.level = (xp.total_xp // 100) + 1
                xp.save()
                self.stdout.write(f'   {user.username}: {old_xp} → {xp.total_xp} XP')
        
        # === 3. Создаём уведомления для достижений ===
        self.stdout.write('\n🔔 Создание уведомлений для достижений...')
        badges_without_notification = 0
        for badge in UserBadge.objects.all():
            exists = Notification.objects.filter(
                user=badge.user,
                notification_type='new_badge',
                message__icontains=badge.get_badge_type_display()
            ).exists()
            
            if not exists:
                Notification.objects.create(
                    user=badge.user,
                    notification_type='new_badge',
                    title='🎖️ Новое достижение!',
                    message=f'Вы получили достижение "{badge.get_badge_type_display()}"',
                    action_url='/users/profile/',
                    is_read=False,
                    created_at=badge.awarded_at
                )
                badges_without_notification += 1
        
        self.stdout.write(self.style.SUCCESS(f'✅ Создано {badges_without_notification} уведомлений о достижениях'))
        
        self.stdout.write('\n✨ Исправление завершено!\n')