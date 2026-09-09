# parsers/tasks.py
"""
2026-09-09: KFF-парсер и вся его инфраструктура (client.py, importers.py,
pipeline.py, photo_scraper.py, все Celery-задачи синхронизации матчей отсюда)
физически удалены по явному решению пользователя — Sportmonks остаётся
ЕДИНСТВЕННЫМ источником данных матчей (см. parsers/sportmonks/tasks.py).

Здесь остаётся только check_sync_errors_and_alert — она НЕ была привязана к
конкретному источнику: читает Match/MatchEvent общими полями (status,
created_at, has_lineup), не знает и не спрашивает, кто именно записал эти
строки (KFF или Sportmonks — см. ParserSyncRun.source, parsers/models.py).
Поэтому это единственная задача из старого файла, которую можно было
оставить как есть, не переписывая заново под Sportmonks.

ВАЖНОЕ ПОСЛЕДСТВИЕ УДАЛЕНИЯ (зафиксировано, чтобы не потерялось): вместе с
parsers/kff/importers.py::import_match_core ушла и единственная реализация
детектора ParserDiscrepancy (правка счёта/статуса задним числом поверх уже
завершённого матча, см. её докстринг в parsers/models.py) — на стороне
Sportmonks-импортёра (parsers/sportmonks/importers.py) аналогичного детектора
НЕТ. Карточка "Расхождения импорта" на /staff/dashboard/data-health/
продолжит работать (сам ParserDiscrepancy.objects.filter(reviewed=False) в
dashboard/services.py ничего не знает про источник), но новых записей в неё
писать больше некому. Если нужно закрыть этот пробел — это отдельная,
самостоятельная задача (добавить эквивалентную проверку в
parsers/sportmonks/importers.py::import_match_core), не восстановление
удалённого кода.

Аналогично: `Match.was_rescheduled` (см. её докстринг в matches/models.py)
раньше проставлялся `_detect_rescheduled_outlier` внутри KFF-шной
update_match_statuses — тоже удалено вместе с задачей. Поле на модели
осталось (историческая разметка уже импортированных матчей не трогается),
но новые переносы дат Sportmonks-эпохи этим способом больше не ловятся.
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
    """Проверка ошибок синхронизации за последние 24 часа и алерт при
    необходимости. Источник-агностично: считает по Match/MatchEvent, не
    важно, кто их создал (Sportmonks — единственный активный синк, но поле
    ParserSyncRun.source в принципе позволяет иметь несколько)."""
    from matches.models import Match

    now = timezone.now()
    cutoff = now - timedelta(hours=24)

    matches_without_lineups = Match.objects.filter(
        status="finished", created_at__gte=cutoff, has_lineup=False
    ).count()

    from events.models import MatchEvent

    matches_without_events = (
        Match.objects.filter(status="finished", created_at__gte=cutoff)
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


def _send_sync_error_alert(error_message: str, alert_type: str, extra_data: dict = None):
    """Отправка email-алерта админу при критических ошибках синка."""
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
