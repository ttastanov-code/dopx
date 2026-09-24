# season_squad/tasks.py
"""Пересчёт «Живой сборной сезона» каждые 15 минут, по всем активным сезонам всех лиг."""
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


@shared_task
def recompute_all_active_best_xi() -> int:
    """Для Celery Beat: задача на каждый активный сезон. Возвращает число задач."""
    from seasons.models import Season

    season_ids = list(Season.objects.filter(is_active=True).values_list('id', flat=True))
    for season_id in season_ids:
        recompute_best_xi_task.delay(str(season_id))
    logger.info("recompute_all_active_best_xi: поставлено %d задач пересчёта", len(season_ids))
    return len(season_ids)
