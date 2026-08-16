# === users/signals.py ===
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone
from evaluations.models import ContextEvaluation
from users.models import User, UserBadge, UserXP
from users.services import check_and_award_badges
import logging

logger = logging.getLogger(__name__)

# Проверка достижений — централизованно в evaluations/views.py после
# полного завершения оценки матча, не через post_save-сигнал (дубликаты
# уведомлений/race conditions при частичном сохранении вайзарда).
# Для асинхронной проверки в других местах: users.tasks.check_badges_async.delay(user_id).