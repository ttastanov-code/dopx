# season_squad/tasks.py
"""Пересчёт «Живой сборной сезона» каждые 15 минут и фиксация итоговой после конца сезона."""
from __future__ import annotations

import logging

from celery import shared_task
from django.core.cache import cache

logger = logging.getLogger(__name__)

# TTL лока на случай падения воркера.
RECOMPUTE_LOCK_TIMEOUT = 300


@shared_task
def recompute_best_xi_task(season_id: str) -> None:
    """Пересчёт одного сезона с Redis-lock (cache.add) — без параллельных пересчётов."""
    lock_key = f"season_squad:recompute:{season_id}"
    if not cache.add(lock_key, "1", timeout=RECOMPUTE_LOCK_TIMEOUT):
        logger.info("recompute_best_xi_task: пересчёт сезона %s уже выполняется — пропускаем", season_id)
        return

    try:
        from seasons.models import Season
        from season_squad.services import recompute_best_xi

        try:
            season = Season.objects.select_related('league').get(pk=season_id)
        except Season.DoesNotExist:
            logger.warning("recompute_best_xi_task: сезон %s не найден (удалён?)", season_id)
            return

        recompute_best_xi(season)
    finally:
        # Снимаем лок сразу.
        cache.delete(lock_key)


def season_is_over(season) -> bool:
    """Сезон неактивен, все его матчи сыграны (или отменены) и голосование по ним закрыто."""
    from django.utils import timezone

    from matches.models import Match

    if season.is_active:
        return False
    matches = Match.objects.filter(season=season)
    return not (
        matches.filter(status__in=('scheduled', 'live')).exists()
        or matches.filter(voting_open_until__gte=timezone.now(), status='finished').exists()
    )


@shared_task
def finalize_season_best_xi_task(season_id: str) -> bool:
    """Последний пересчёт и фиксация итоговой сборной завершённого сезона."""
    from seasons.models import Season
    from season_squad.services import finalize_best_xi, recompute_best_xi

    season = Season.objects.select_related('league').filter(pk=season_id).first()
    if season is None or not season_is_over(season):
        return False
    recompute_best_xi(season)
    finalize_best_xi(season)
    logger.info("Итоговая сборная сезона %s зафиксирована", season)
    return True


@shared_task
def recompute_all_active_best_xi() -> int:
    """Для Celery Beat: пересчёт живых сборных — активных сезонов и только что закончившихся
    (пока по ним идёт голосование); закончившиеся полностью фиксируются как итоговые.
    """
    from seasons.models import Season
    from season_squad.models import SeasonBestXI

    final_ids = set(SeasonBestXI.objects.filter(is_final=True).values_list('season_id', flat=True))
    queued = 0
    for season in Season.objects.filter(match__isnull=False).distinct():
        if season.id in final_ids:
            continue
        if season.is_active:
            recompute_best_xi_task.delay(str(season.id))
        elif season_is_over(season):
            finalize_season_best_xi_task.delay(str(season.id))
        else:
            # Сезон уже не активен, но голосование по последним матчам ещё идёт.
            recompute_best_xi_task.delay(str(season.id))
        queued += 1
    logger.info("recompute_all_active_best_xi: поставлено %d задач", queued)
    return queued
