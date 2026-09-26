# === users/signals.py ===
from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver
from django.utils import timezone
from evaluations.models import ContextEvaluation, EvaluationSession
from predictions.models import MatchPrediction
from users.models import User, UserBadge, UserXP
from users.services import check_and_award_badges
import logging

logger = logging.getLogger(__name__)

# Достижения проверяются после завершения оценки (evaluations/views.py), не сигналом.


@receiver(post_delete, sender=EvaluationSession)
@receiver(post_delete, sender=MatchPrediction)
def on_progress_source_deleted(sender, instance, **kwargs):
    """Удалили оценку или прогноз — XP, серии и достижения пересобираем из оставшихся данных."""
    from users.tasks import schedule_progress_recompute

    user_id = str(instance.user_id)
    transaction.on_commit(lambda: schedule_progress_recompute(user_id))
