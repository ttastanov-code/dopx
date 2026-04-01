# === НОВЫЙ ФАЙЛ: users/signals.py ===
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone
from evaluations.models import ContextEvaluation
from users.models import User, UserBadge, UserXP
from users.services import check_and_award_badges
import logging

logger = logging.getLogger(__name__)

@receiver(post_save, sender=ContextEvaluation)
def on_context_evaluation_created(sender, instance, created, **kwargs):
    """
    При создании новой оценки контекста:
    - Проверяем и выдаём достижения
    - Обновляем статистику
    """
    if not created:
        return
    
    user = instance.user
    
    # Проверяем достижения асинхронно (чтобы не замедлять запрос)
    from users.tasks import check_badges_async
    check_badges_async.delay(str(user.id))


# === НОВЫЙ ФАЙЛ: users/tasks.py ===
from celery import shared_task
from users.models import User
from users.services import check_and_award_badges
import logging

logger = logging.getLogger(__name__)

@shared_task(bind=True, max_retries=3)
def check_badges_async(self, user_id: str):
    """Асинхронная проверка достижений"""
    try:
        user = User.objects.get(id=user_id)
        awarded = check_and_award_badges(user)
        
        if awarded:
            logger.info(f"Awarded {len(awarded)} badges to {user.username}")
            # Отправляем уведомления о каждом достижении
            from notifications.tasks import send_badge_earned_notification
            for badge in awarded:
                send_badge_earned_notification.delay(
                    user_id=user_id,
                    badge_type=badge.badge_type,
                    badge_name=badge.get_badge_type_display()
                )
        
        return {'awarded': len(awarded)}
    except User.DoesNotExist:
        logger.error(f"User {user_id} not found")
        return {'error': 'User not found'}
    except Exception as e:
        logger.error(f"Error checking badges: {e}", exc_info=True)
        raise self.retry(exc=e, countdown=60)