# dashboard/access.py
"""Доступ к разделам дашборда. user_can_access_section() используют
мидлварь (реальная проверка) и тег в _nav.html (скрытие вкладок).
"""
from __future__ import annotations

from .models import StaffAccessGrant

# Порядок важен: специфичные префиксы раньше корня.
SECTION_PATH_MAP: list[tuple[str, str]] = [
    ("/staff/dashboard/security/", ""),  # 2FA — всегда доступно
    ("/staff/dashboard/access/", "access_roles"),
    ("/staff/dashboard/traffic/", "traffic"),
    ("/staff/dashboard/matches/", "matches"),
    ("/staff/dashboard/data-health/", "data_health"),
    ("/staff/dashboard/data-trust/", "data_trust"),
    ("/staff/dashboard/duplicate-players/", "duplicate_players"),
    ("/staff/dashboard/names-review/", "names_review"),
    ("/staff/dashboard/evaluations/", "evaluation_sessions"),
    ("/staff/dashboard/users/", "users"),
    ("/staff/dashboard/antifraud/", "antifraud"),
    ("/staff/dashboard/parser/", "parser_tools"),
    # partners/banners — подраздел «Реклама»
    ("/staff/dashboard/partners/", "ads"),
    ("/staff/dashboard/banners/", "ads"),
    ("/staff/dashboard/ads/", "ads"),
    ("/staff/dashboard/audit/", "audit"),
    ("/staff/dashboard/announcements/", "announcements"),
    ("/staff/dashboard/settings/", "platform_settings"),
    ("/staff/dashboard/system-status/", "system_status"),
    ("/staff/dashboard/scripts/", "scripts"),
]

# access_roles — только суперпользователь.
SUPERUSER_ONLY_SECTIONS = {"access_roles"}


def resolve_section_for_path(path: str) -> str | None:
    """Ключ раздела для пути; "" — всегда разрешено; None — вне дашборда."""
    for prefix, section in SECTION_PATH_MAP:
        if path.startswith(prefix):
            return section
    if path.startswith("/staff/dashboard/"):
        return "overview"
    return None


def user_can_access_section(user, section_key: str) -> bool:
    if not section_key:
        return True
    if getattr(user, "is_superuser", False):
        return True
    if section_key in SUPERUSER_ONLY_SECTIONS:
        return False
    try:
        grant = user.dashboard_access_grant
    except StaffAccessGrant.DoesNotExist:
        # Нет записи — доступа нет: разделы выдаёт суперпользователь явно.
        return False
    return grant.has_section(section_key)
