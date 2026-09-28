# core/tasks.py
"""Инфраструктурные периодические задачи."""
from __future__ import annotations

import logging

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger(__name__)


@shared_task
def cleanup_expired_captchas() -> int:
    """Удаляет просроченные CaptchaStore. Возвращает число удалённых."""
    from captcha.models import CaptchaStore

    deleted, _ = CaptchaStore.objects.filter(expiration__lt=timezone.now()).delete()
    if deleted:
        logger.info("Удалено %d просроченных captcha-записей.", deleted)
    return deleted


HEARTBEAT_CACHE_KEY = "celery:heartbeat"


@shared_task(ignore_result=True)
def celery_heartbeat():
    """Раз в минуту: отметка «beat и воркер живы» для /healthz/ и дашборда."""
    from django.core.cache import cache
    from django.utils import timezone

    cache.set(HEARTBEAT_CACHE_KEY, timezone.now().isoformat(), 60 * 60)
