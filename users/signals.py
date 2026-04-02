# === users/signals.py ===
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone
from evaluations.models import ContextEvaluation
from users.models import User, UserBadge, UserXP
from users.services import check_and_award_badges
import logging

logger = logging.getLogger(__name__)

# ✅ ИСПРАВЛЕНО: Убран post_save сигнал на ContextEvaluation.
# Проверка достижений и уведомления теперь централизованно обрабатываются 
# в evaluations/views.py после полного завершения оценки матча. 
# Это предотвращает дубликаты уведомлений, race conditions и ошибки Celery.
# 
# Если вам нужна асинхронная проверка достижений в других местах,
# используйте users.tasks.check_badges_async.delay(user_id) напрямую.