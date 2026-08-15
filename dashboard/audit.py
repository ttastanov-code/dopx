# dashboard/audit.py
"""Единая точка записи в StaffActionLog — используется ВЕЗДЕ вместо
`StaffActionLog.objects.create()` напрямую (тот же принцип, что
analytics.services.track_event для AnalyticsEvent)."""
from __future__ import annotations

import logging
from typing import Any

from django.http import HttpRequest

from core.utils import get_client_ip
from dashboard.models import AuditAction, StaffActionLog

logger = logging.getLogger(__name__)


def log_staff_action(
    request: HttpRequest,
    action: AuditAction | str,
    *,
    target: str = "",
    details: dict[str, Any] | None = None,
) -> None:
    """Синхронная запись (не Celery) — экшены staff единичны и редки
    (десятки в день, не тысячи в секунду как продуктовая аналитика), лишний
    async-хоп через очередь тут не нужен и только замедлил бы обратную связь
    в UI (staff должен видеть свой же экшен в аудит-логе сразу после клика).

    НЕ бросаем исключения наружу — сбой записи аудита не должен блокировать
    сам экшен (подтверждение флага/ресинк матча всё равно должны отработать).
    """
    try:
        StaffActionLog.objects.create(
            actor=request.user if request.user.is_authenticated else None,
            actor_username=request.user.username if request.user.is_authenticated else "",
            action=action,
            target=target[:300],
            details=details or {},
            ip_address=get_client_ip(request),
        )
    except Exception:
        logger.error("log_staff_action: не удалось записать аудит-лог", exc_info=True)
