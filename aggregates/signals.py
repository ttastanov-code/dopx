# aggregates/signals.py
from django.core.cache import cache
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver
from django.utils import timezone
from matches.models import Match
from evaluations.models import (
    CoachEvaluation,
    MatchEvaluation,
    PlayerEvaluation,
    RefereeEvaluation,
    TeamEvaluation,
)
from aggregates.tasks import trigger_aggregate_recalculation, recalculate_season_standings
import logging

logger = logging.getLogger(__name__)


def _schedule_recalculation(match_id: str, countdown: int) -> None:
    """Ставит пересчёт агрегатов матча не чаще раза в countdown секунд.
    cache.add() как атомарный SETNX — без дублей задач в очереди.
    """
    debounce_key = f"aggregates:recalc_pending:{match_id}"
    if not cache.add(debounce_key, "1", timeout=countdown):
        return
    trigger_aggregate_recalculation.apply_async(args=[match_id], countdown=countdown)


@receiver(post_save, sender=Match)
def on_match_status_changed(sender, instance, **kwargs):
    """Матч завершён -> пересчёт таблицы, с той же дедупликацией через cache.add()."""
    if instance.status == 'finished' and instance.season:
        debounce_key = f"aggregates:standings_recalc_pending:{instance.season.id}"
        if not cache.add(debounce_key, "1", timeout=60):
            return
        # Откладываем на минуту, чтобы собрать несколько изменений
        recalculate_season_standings.apply_async(
            args=[instance.season.id],
            countdown=60
        )

@receiver(post_save, sender=PlayerEvaluation)
def on_player_evaluation_saved(sender, instance, created, **kwargs):
    """Оценка игрока -> пересчёт агрегатов."""
    match_id = str(instance.match.id)
    logger.info(f"PlayerEvaluation saved, triggering recalculation for match {match_id}")

    # Откладываем на 30 с; дедупликация — в _schedule_recalculation
    _schedule_recalculation(match_id, countdown=30)


@receiver(post_save, sender=MatchEvaluation)
def on_match_evaluation_saved(sender, instance, created, **kwargs):
    """Оценка матча -> пересчёт агрегатов."""
    match_id = str(instance.match.id)
    logger.info(f"MatchEvaluation saved, triggering recalculation for match {match_id}")

    _schedule_recalculation(match_id, countdown=30)


@receiver(post_save, sender=CoachEvaluation)
def on_coach_evaluation_saved(sender, instance, created, **kwargs):
    """Оценка тренера -> пересчёт агрегатов."""
    match_id = str(instance.match.id)
    logger.info(f"CoachEvaluation saved, triggering recalculation for match {match_id}")

    _schedule_recalculation(match_id, countdown=30)


@receiver(post_save, sender=TeamEvaluation)
def on_team_evaluation_saved(sender, instance, created, **kwargs):
    """Оценка команды -> пересчёт агрегатов."""
    match_id = str(instance.match.id)
    logger.info(f"TeamEvaluation saved, triggering recalculation for match {match_id}")

    _schedule_recalculation(match_id, countdown=30)


@receiver(post_save, sender=RefereeEvaluation)
def on_referee_evaluation_saved(sender, instance, created, **kwargs):
    """Оценка судьи -> пересчёт агрегатов."""
    match_id = str(instance.match.id)
    logger.info(f"RefereeEvaluation saved, triggering recalculation for match {match_id}")

    _schedule_recalculation(match_id, countdown=30)


@receiver(post_save, sender=Match)
def on_match_voting_deadline_changed(sender, instance, created=False, update_fields=None, **kwargs):
    """Изменение voting_open_until -> пересчёт."""
    # Пропускаем при создании
    if created:
        return
    
    # update_fields может быть None
    if update_fields and 'voting_open_until' in update_fields:
        match_id = str(instance.id)
        logger.info(f"Match voting deadline changed, triggering recalculation for {match_id}")
        
        # Голосование закрылось — пересчёт сразу
        if instance.voting_open_until <= timezone.now():
            trigger_aggregate_recalculation.delay(match_id)
        else:
            _schedule_recalculation(match_id, countdown=60)


@receiver(post_delete, sender=PlayerEvaluation)
@receiver(post_delete, sender=MatchEvaluation)
@receiver(post_delete, sender=CoachEvaluation)
@receiver(post_delete, sender=TeamEvaluation)
@receiver(post_delete, sender=RefereeEvaluation)
def on_evaluation_deleted(sender, instance, **kwargs):
    """Удалённая оценка -> пересчёт (устаревшие агрегаты удалит сам пересчёт)."""
    _schedule_recalculation(str(instance.match_id), countdown=30)
