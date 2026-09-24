# dashboard/audit.py
"""Запись в StaffActionLog — только через log_staff_action()."""
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
    """Синхронная запись. Исключения не пробрасываются — сбой аудита не блокирует действие."""
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
