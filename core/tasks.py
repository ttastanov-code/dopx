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


def _worker_beat(task, name: str) -> None:
    """Пульс воркера и beat (задачу поставил beat — значит, он жив); флаг перезапуска — мягкая остановка воркера."""
    from core import heartbeat

    heartbeat.beat(name, node=task.request.hostname)
    heartbeat.beat("celery_beat")
    if heartbeat.restart_requested(name):
        from celery import current_app
        current_app.control.shutdown(destination=[task.request.hostname])  # Docker поднимет заново


@shared_task(bind=True, ignore_result=True)
def celery_heartbeat(self):
    """Раз в минуту: отметка «beat и воркер живы» для /healthz/ и дашборда."""
    from django.core.cache import cache
    from django.utils import timezone

    cache.set(HEARTBEAT_CACHE_KEY, timezone.now().isoformat(), 60 * 60)
    _worker_beat(self, "celery_worker")


@shared_task(bind=True, ignore_result=True)
def realtime_heartbeat(self):
    """То же для воркера очереди realtime (live-матчи и пуши)."""
    _worker_beat(self, "celery_realtime")
