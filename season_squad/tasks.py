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
from django.core.cache import cache

logger = logging.getLogger(__name__)

# Максимальное время удержания лока — страховка на случай, если воркер
# упадёт/будет убит посреди пересчёта и не дойдёт до cache.delete() в
# finally: ключ сам протухнет через RECOMPUTE_LOCK_TIMEOUT секунд, а не
# зависнет навечно. Значение с большим запасом от реального времени
# пересчёта одного сезона (обычно единицы секунд).
RECOMPUTE_LOCK_TIMEOUT = 300


@shared_task
def recompute_best_xi_task(season_id: str) -> None:
    """Пересчёт одного сезона — отдельная задача (не инлайн-цикл в
    recompute_all_active_best_xi), чтобы зависание/ошибка на одном сезоне
    не блокировала пересчёт остальных и ретраилась Celery независимо.

    Redis-lock (продуктовый ревью 2026-08-22): без него два пересчёта
    ОДНОГО сезона могли выполниться параллельно — например, Celery Beat
    сработал ровно в момент, когда staff вручную нажал "пересчитать
    сейчас" в дашборде, или сообщение доставилось дважды (at-least-once
    delivery). recompute_best_xi() внутри читает предыдущие ранги из
    SeasonPositionRanking ДО того как записать новую партию — при гонке
    два прогона могли прочитать одну и ту же "предыдущую" партию и оба
    записать историю рангов, из-за чего rank_change/rank_change_delta
    (стрелки ↑/"вошёл в состав" на карточках) считались бы некорректно.
    cache.add() — тот же атомарный SETNX-паттерн, что и в
    aggregates/signals.py::_schedule_recalculation для дебаунса пересчёта
    агрегатов матча."""
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
        # Явное освобождение сразу по завершении — не ждём TTL, чтобы
        # следующий легитимный пересчёт (ручной триггер сразу после
        # планового) не блокировался лишние 5 минут.
        cache.delete(lock_key)


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
