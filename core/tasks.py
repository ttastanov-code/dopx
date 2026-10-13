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


# Журнал запусков синхронизации: live-опрос пишет запись каждые 10 с.
SYNC_RUN_KEEP_DAYS = 14
SYNC_RUN_KEEP_DAYS_WITH_ERRORS = 60


@shared_task
def cleanup_old_sync_runs() -> int:
    """Удаляет старые ParserSyncRun: успешные — через 14 дней, с ошибками — через 60."""
    from datetime import timedelta

    from django.db.models import Q

    from parsers.models import ParserSyncRun

    now = timezone.now()
    deleted, _ = ParserSyncRun.objects.filter(
        Q(created_at__lt=now - timedelta(days=SYNC_RUN_KEEP_DAYS), errors=0)
        | Q(created_at__lt=now - timedelta(days=SYNC_RUN_KEEP_DAYS_WITH_ERRORS))
    ).delete()
    if deleted:
        logger.info("Удалено %d старых записей журнала синхронизации.", deleted)
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

    from dashboard.models import ManagementCommandRun

    ManagementCommandRun.mark_interrupted()  # оборванные перезапуском скрипты не висят «Выполняется» вечно


@shared_task(bind=True, ignore_result=True)
def realtime_heartbeat(self):
    """То же для воркера очереди realtime (live-матчи и пуши)."""
    _worker_beat(self, "celery_realtime")


@shared_task(ignore_result=True)
def data_integrity_audit():
    """Ежедневная проверка целостности данных (core/integrity.py); ошибки видит бот и «Здоровье данных»."""
    from core.integrity import run_and_store

    report = run_and_store()
    if report["errors"]:
        logger.warning("Целостность данных: ошибок %s, предупреждений %s", report["errors"], report["warnings"])
    return {"errors": report["errors"], "warnings": report["warnings"]}
