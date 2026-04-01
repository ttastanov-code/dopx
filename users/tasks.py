# users/tasks.py
from celery import shared_task
from .services import check_and_award_badges
from django.contrib.auth import get_user_model
import logging

logger = logging.getLogger(__name__)
User = get_user_model()

@shared_task(bind=True, max_retries=3)
def check_badges_async(self, user_id: str):
    """Асинхронная проверка достижений пользователя"""
    try:
        user = User.objects.get(id=user_id)
        return check_and_award_badges(user)
    except User.DoesNotExist:
        logger.error(f"User {user_id} not found")
        return None