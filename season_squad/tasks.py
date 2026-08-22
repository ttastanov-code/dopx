# season_squad/tasks.py
"""
Периодический пересчёт "Живой сборной сезона" (см. CELERY_BEAT_SCHEDULE
в dopx/settings.py::'recompute-live-best-xi', каждые 15 минут).

recompute_all_active_best_xi() специально считает по ВСЕМ активным сезонам
всех лиг, а не только по главной (League.is_primary) — сайт мультисезонный
(см. players/views.py и сезонные фильтры, задача #143/#147 в истории
проекта), у второстепенной лиги тоже может быть смысл показывать свою
живую сборную на её собственной странице /leagues/<id>/best-xi/.
"""
from __future__ import annotations

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task
def recompute_best_xi_task(season_id: str) -> None:
    """Пересчёт одного сезона — отдельная задача (не инлайн-цикл в
    recompute_all_active_best_xi), чтобы зависание/ошибка на одном сезоне
    не блокировала пересчёт остальных и ретраилась Celery независимо."""
    from seasons.models import Season
    from season_squad.services import recompute_best_xi

    try:
        season = Season.objects.select_related('league').get(pk=season_id)
    except Season.DoesNotExist:
        logger.warning("recompute_best_xi_task: сезон %s не найден (удалён?)", season_id)
        return

    recompute_best_xi(season)


@shared_task
def recompute_all_active_best_xi() -> int:
    """Точка входа для Celery Beat — ставит в очередь по одной задаче на
    каждый активный сезон. Возвращает число поставленных задач (видно в
    Celery-логах и в ручном запуске из dashboard, см. dashboard/views.py
    ::run_celery_task)."""
    from seasons.models import Season

    season_ids = list(Season.objects.filter(is_active=True).values_list('id', flat=True))
    for season_id in season_ids:
        recompute_best_xi_task.delay(str(season_id))
    logger.info("recompute_all_active_best_xi: поставлено %d задач пересчёта", len(season_ids))
    return len(season_ids)
