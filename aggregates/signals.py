# aggregates/signals.py
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone
from matches.models import Match
from evaluations.models import PlayerEvaluation, MatchEvaluation, CoachEvaluation
from aggregates.tasks import trigger_aggregate_recalculation
import logging

logger = logging.getLogger(__name__)


@receiver(post_save, sender=PlayerEvaluation)
def on_player_evaluation_saved(sender, instance, created, **kwargs):
    """
    При создании/обновлении оценки игрока запускаем пересчёт агрегатов
    """
    match_id = str(instance.match.id)
    logger.info(f"PlayerEvaluation saved, triggering recalculation for match {match_id}")
    
    # Откладываем задачу на 30 секунд чтобы собрать больше изменений
    trigger_aggregate_recalculation.apply_async(
        args=[match_id],
        countdown=30
    )


@receiver(post_save, sender=MatchEvaluation)
def on_match_evaluation_saved(sender, instance, created, **kwargs):
    """
    При создании/обновлении оценки матча запускаем пересчёт агрегатов
    """
    match_id = str(instance.match.id)
    logger.info(f"MatchEvaluation saved, triggering recalculation for match {match_id}")
    
    trigger_aggregate_recalculation.apply_async(
        args=[match_id],
        countdown=30
    )


@receiver(post_save, sender=CoachEvaluation)
def on_coach_evaluation_saved(sender, instance, created, **kwargs):
    """
    При создании/обновлении оценки тренера запускаем пересчёт агрегатов
    """
    match_id = str(instance.match.id)
    logger.info(f"CoachEvaluation saved, triggering recalculation for match {match_id}")
    
    trigger_aggregate_recalculation.apply_async(
        args=[match_id],
        countdown=30
    )


@receiver(post_save, sender=Match)
def on_match_voting_deadline_changed(sender, instance, created=False, update_fields=None, **kwargs):
    """
    При изменении voting_open_until запускаем пересчёт
    """
    # Пропускаем при создании объекта
    if created:
        return
    
    # update_fields может быть None, если save() вызван без этого параметра
    if update_fields and 'voting_open_until' in update_fields:
        match_id = str(instance.id)
        logger.info(f"Match voting deadline changed, triggering recalculation for {match_id}")
        
        # Если голосование закрылось - немедленный пересчёт
        if instance.voting_open_until <= timezone.now():
            trigger_aggregate_recalculation.delay(match_id)
        else:
            trigger_aggregate_recalculation.apply_async(
                args=[match_id],
                countdown=60
            )