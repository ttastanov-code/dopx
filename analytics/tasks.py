# analytics/tasks.py
from __future__ import annotations

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=10)
def persist_event_task(self, payload: dict) -> None:
    """Запись AnalyticsEvent. Ошибки валидации проглатываются."""
    from analytics.models import AnalyticsEvent

    try:
        AnalyticsEvent.objects.create(**payload)
    except Exception as exc:
        logger.warning("Analytics persist failed: %s | payload=%s", exc, payload)
        raise self.retry(exc=exc)


# Массовые события: сырые храним ANALYTICS_RAW_DAYS (дашборд смотрит максимум 90 дней), остальные — ANALYTICS_KEEP_DAYS (0 — вечно).
# Оценки, прогнозы, рейтинги и аккаунты живут в своих таблицах — эта чистка их не касается.
HIGH_VOLUME = ("page_view", "banner_impression", "share_card_viewed", "widget_embed_viewed",
               "leaderboard_viewed", "profile_viewed", "wizard_step_completed")
PURGE_BATCH = 10_000


def rollup_day(day) -> int:
    """Пересчитать AnalyticsDailyStat за день (идемпотентно). Возвращает число строк."""
    from datetime import datetime, time, timedelta

    from django.db.models import Count, Q
    from django.utils import timezone

    from analytics.models import AnalyticsDailyStat, AnalyticsEvent

    start = timezone.make_aware(datetime.combine(day, time.min))
    rows = (AnalyticsEvent.objects.filter(created_at__gte=start, created_at__lt=start + timedelta(days=1))
            .values("event_name")
            .annotate(events=Count("id"), visitors=Count("anonymous_id", distinct=True),
                      users=Count("user", distinct=True, filter=Q(user__isnull=False))))
    for r in rows:
        AnalyticsDailyStat.objects.update_or_create(
            date=day, event_name=r["event_name"],
            defaults={"events": r["events"], "visitors": r["visitors"], "users": r["users"]})
    return len(rows)


@shared_task
def analytics_maintenance() -> dict:
    """Ночью: итоги за последние дни и за ещё не свёрнутые старые дни, затем удаление старых сырых событий."""
    from datetime import timedelta

    from django.conf import settings
    from django.db.models.functions import TruncDate
    from django.utils import timezone

    from analytics.models import AnalyticsDailyStat, AnalyticsEvent

    today = timezone.localdate()
    raw_cut = timezone.now() - timedelta(days=settings.ANALYTICS_RAW_DAYS)
    keep_days = settings.ANALYTICS_KEEP_DAYS
    # Дни, которые сейчас удалятся, обязательно сворачиваем, даже если ночная задача раньше не работала.
    done = set(AnalyticsDailyStat.objects.values_list("date", flat=True).distinct())
    old_days = set(AnalyticsEvent.objects.filter(created_at__lt=raw_cut).annotate(d=TruncDate("created_at"))
                   .values_list("d", flat=True).distinct())
    days = {today - timedelta(days=i) for i in range(1, 4)} | (old_days - done)
    for day in sorted(days):
        rollup_day(day)

    deleted = 0
    targets = [AnalyticsEvent.objects.filter(event_name__in=HIGH_VOLUME, created_at__lt=raw_cut)]
    if keep_days:
        targets.append(AnalyticsEvent.objects.filter(created_at__lt=timezone.now() - timedelta(days=keep_days)))
    for qs in targets:
        while True:
            ids = list(qs.values_list("id", flat=True)[:PURGE_BATCH])
            if not ids:
                break
            deleted += AnalyticsEvent.objects.filter(id__in=ids).delete()[0]
    logger.info("analytics_maintenance: свёрнуто дней %d, удалено событий %d", len(days), deleted)
    return {"days": len(days), "deleted": deleted}
