# parsers/tasks.py
"""Общие задачи парсера, не зависящие от источника: алерт по ошибкам синка,
ежемесячная проверка ФИО через ИИ.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.html import strip_tags

logger = logging.getLogger(__name__)


@shared_task
def check_sync_errors_and_alert():
    """Ошибки синка за 24 часа и алерт при необходимости."""
    from matches.models import Match

    now = timezone.now()
    cutoff = now - timedelta(hours=24)

    # Технические результаты (decided_administratively) без составов — не ошибка.
    matches_without_lineups = Match.objects.filter(
        status="finished",
        created_at__gte=cutoff,
        has_lineup=False,
        decided_administratively=False,
    ).count()

    from events.models import MatchEvent

    matches_without_events = (
        Match.objects.filter(
            status="finished", created_at__gte=cutoff, decided_administratively=False
        )
        .exclude(
            id__in=MatchEvent.objects.filter(created_at__gte=cutoff).values_list(
                "match_id", flat=True
            )
        )
        .count()
    )

    threshold_lineups = 5
    threshold_events = 10

    alerts = []

    if matches_without_lineups > threshold_lineups:
        alerts.append(f"⚠️ {matches_without_lineups} матчей без составов за 24ч")

    if matches_without_events > threshold_events:
        alerts.append(f"⚠️ {matches_without_events} матчей без событий за 24ч")

    if alerts:
        error_msg = "Проблемы с синхронизацией:\n" + "\n".join(alerts)
        logger.warning(error_msg)
        _send_sync_error_alert(
            error_msg,
            "sync_monitoring",
            extra_data={
                "matches_without_lineups": matches_without_lineups,
                "matches_without_events": matches_without_events,
            },
        )
        return {"status": "alert_sent", "alerts": alerts}

    logger.info("✅ Sync monitoring: No critical issues detected")
    return {"status": "ok"}


@shared_task(bind=True, max_retries=0)
def verify_names_with_ai_monthly(self):
    """Ежемесячный прогон verify_names_with_ai (только новые/упавшие записи, limit=100).
    Пишет ManagementCommandRun с triggered_by «celery-beat (ежемесячно)».
    """
    from dashboard.command_runner import run_command_sync
    from dashboard.commands_registry import get_command
    from dashboard.models import ManagementCommandRun

    spec = get_command("verify_names_with_ai")
    if spec is None:
        logger.error("verify_names_with_ai_monthly: команда verify_names_with_ai не найдена в COMMAND_REGISTRY")
        return {"status": "error", "reason": "command_not_registered"}

    positional: list = []
    kwargs = {"entity": None, "limit": 100, "delay": 4.0, "recheck": False, "dry_run": False}

    run = ManagementCommandRun.objects.create(
        command_name=spec.name,
        args={"positional": positional, "kwargs": kwargs},
        status=ManagementCommandRun.Status.RUNNING,
        triggered_by=None,
        triggered_by_username="celery-beat (ежемесячно)",
        started_at=timezone.now(),
        celery_task_id=self.request.id or "",
    )

    success, out, err = run_command_sync(spec, positional, kwargs)

    run.status = ManagementCommandRun.Status.SUCCESS if success else ManagementCommandRun.Status.FAILED
    run.stdout, run.stderr = out, err
    run.finished_at = timezone.now()
    run.save(update_fields=["status", "stdout", "stderr", "finished_at"])

    return {"status": "success" if success else "failed", "run_id": str(run.id)}


def _send_sync_error_alert(error_message: str, alert_type: str, extra_data: dict = None):
    """Email-алерт админу."""
    if not getattr(settings, "ENABLE_SYNC_ERROR_ALERTS", True):
        return

    admin_email = getattr(settings, "ADMIN_ALERT_EMAIL", settings.CONTACT_EMAIL)
    site_url = getattr(settings, "SITE_URL", "https://dopx.kz")

    subject = f"DOPX Sync Alert [{alert_type}]"

    html_message = render_to_string(
        "emails/sync_error_alert.html",
        {
            "error_message": error_message,
            "alert_type": alert_type,
            "extra_data": extra_data,
            "timestamp": timezone.now(),
            "site_url": site_url,
        },
    )

    try:
        send_mail(
            subject=subject,
            message=strip_tags(html_message),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[admin_email],
            html_message=html_message,
            fail_silently=True,
        )
        logger.info(f"✅ Sync error alert sent to {admin_email}")
    except Exception as e:
        logger.error(f"❌ Failed to send sync error alert: {e}")
