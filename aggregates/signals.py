# aggregates/signals.py
from django.core.cache import cache
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone
from matches.models import Match
from evaluations.models import PlayerEvaluation, MatchEvaluation, CoachEvaluation
from aggregates.tasks import trigger_aggregate_recalculation, recalculate_season_standings
import logging

logger = logging.getLogger(__name__)


def _schedule_recalculation(match_id: str, countdown: int) -> None:
    """Ставит пересчёт агрегатов матча в очередь МАКСИМУМ раз в `countdown`
    секунд на матч — раньше каждое сохранение PlayerEvaluation/
    MatchEvaluation/CoachEvaluation ставило СВОЮ отдельную отложенную
    задачу без дедупликации: при массовом создании тестовых оценок (много
    saves за секунды) в очереди Celery копились тысячи избыточных
    пересчётов ОДНОГО И ТОГО ЖЕ матча, вставая в очереди впереди вообще
    всех остальных задач и блокируя их на часы вперёд — реальный инцидент
    2026-08-22: очередь 'celery' разрослась до 42105 сообщений,
    'Сборная DOPX' не пересчитывалась много часов не из-за бага в её
    коде, а потому что до её задачи в очереди просто не доходила очередь.

    cache.add() — атомарный SETNX: если пересчёт для этого матча уже
    запланирован (ключ ещё не истёк), просто выходим — очередная оценка
    "подхватится" уже запланированным пересчётом, отдельная задача не
    нужна. timeout=countdown — ключ живёт ровно до момента, когда
    запланированная задача должна была запуститься, дальше следующее
    сохранение сможет поставить новую."""
    debounce_key = f"aggregates:recalc_pending:{match_id}"
    if not cache.add(debounce_key, "1", timeout=countdown):
        return
    trigger_aggregate_recalculation.apply_async(args=[match_id], countdown=countdown)


@receiver(post_save, sender=Match)
def on_match_status_changed(sender, instance, **kwargs):
    """При изменении статуса матча → пересчёт таблицы"""
    if instance.status == 'finished' and instance.season:
        # Откладываем на 1 минуту чтобы собрать несколько изменений
        recalculate_season_standings.apply_async(
            args=[instance.season.id],
            countdown=60
        )

@receiver(post_save, sender=PlayerEvaluation)
def on_player_evaluation_saved(sender, instance, created, **kwargs):
    """
    При создании/обновлении оценки игрока запускаем пересчёт агрегатов
    """
    match_id = str(instance.match.id)
    logger.info(f"PlayerEvaluation saved, triggering recalculation for match {match_id}")

    # Откладываем задачу на 30 секунд чтобы собрать больше изменений —
    # _schedule_recalculation дедуплицирует, см. её докстринг
    _schedule_recalculation(match_id, countdown=30)


@receiver(post_save, sender=MatchEvaluation)
def on_match_evaluation_saved(sender, instance, created, **kwargs):
    """
    При создании/обновлении оценки матча запускаем пересчёт агрегатов
    """
    match_id = str(instance.match.id)
    logger.info(f"MatchEvaluation saved, triggering recalculation for match {match_id}")

    _schedule_recalculation(match_id, countdown=30)


@receiver(post_save, sender=CoachEvaluation)
def on_coach_evaluation_saved(sender, instance, created, **kwargs):
    """
    При создании/обновлении оценки тренера запускаем пересчёт агрегатов
    """
    match_id = str(instance.match.id)
    logger.info(f"CoachEvaluation saved, triggering recalculation for match {match_id}")

    _schedule_recalculation(match_id, countdown=30)


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
            _schedule_recalculation(match_id, countdown=60)