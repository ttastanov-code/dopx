# users/tasks.py
from celery import shared_task
from .services import check_and_award_badges
from django.contrib.auth import get_user_model
import logging

logger = logging.getLogger(__name__)
User = get_user_model()

@shared_task(bind=True, max_retries=3)
def check_badges_async(self, user_id: str):
    """
    Асинхронная проверка достижений пользователя.
    ✅ ИСПРАВЛЕНО: Убрана дублирующая отправка email-уведомлений.
    Теперь уведомления отправляются централизованно из evaluations/views.py после завершения оценки.
    """
    try:
        user = User.objects.get(id=user_id)
        awarded = check_and_award_badges(user)
        logger.info(f"Badge check completed for {user.username}. Awarded: {len(awarded)}")
        return {'awarded': len(awarded)}
    except User.DoesNotExist:
        logger.error(f"User {user_id} not found")
        return None
    except Exception as e:
        logger.error(f"Error checking badges: {e}", exc_info=True)
        raise self.retry(exc=e, countdown=60)