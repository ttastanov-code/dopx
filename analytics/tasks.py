# analytics/tasks.py
from __future__ import annotations

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=10)
def persist_event_task(self, payload: dict) -> None:
    """
    Единственный писатель в AnalyticsEvent. Намеренно глотает ошибки
    валидации данных вместо падения воркера целиком — аналитика
    вспомогательная система, потеря одного события не должна быть заметна
    пользователю (track_event уже вызван синхронно в его view).
    """
    from analytics.models import AnalyticsEvent

    try:
        AnalyticsEvent.objects.create(**payload)
    except Exception as exc:
        logger.warning("Analytics persist failed: %s | payload=%s", exc, payload)
        raise self.retry(exc=exc)
