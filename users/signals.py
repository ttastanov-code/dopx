# === users/signals.py ===
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone
from evaluations.models import ContextEvaluation
from users.models import User, UserBadge, UserXP
from users.services import check_and_award_badges
import logging

logger = logging.getLogger(__name__)

# Достижения проверяются после завершения оценки (evaluations/views.py), не сигналом.
