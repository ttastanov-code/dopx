# dopx/admin_nav.py
"""Пункты и проверки прав для сайдбара Unfold (UNFOLD["SIDEBAR"] в settings).
Без permission Unfold показывает пункт всем, а при переходе — 403.
Модели здесь не импортируются: модуль грузится вместе с settings.
"""
from __future__ import annotations

from typing import Callable

from django.http import HttpRequest

_model_admins: dict[str, object] | None = None


def _model_admin(key: str):
    """ModelAdmin по ключу «app_label_modelname» из admin:<key>_changelist."""
    global _model_admins
    if _model_admins is None:
        from django.contrib import admin

        _model_admins = {
            f"{m._meta.app_label}_{m._meta.model_name}": ma for m, ma in admin.site._registry.items()
        }
    return _model_admins.get(key)


def admin_perm(key: str) -> Callable[[HttpRequest], bool]:
    """Пункт виден, если ModelAdmin разрешает просмотр или изменение."""
    def check(request: HttpRequest) -> bool:
        model_admin = _model_admin(key)
        return bool(model_admin and model_admin.has_view_or_change_permission(request))
    return check


def dashboard_perm(section: str) -> Callable[[HttpRequest], bool]:
    """Пункт виден, если раздел дашборда открыт сотруднику."""
    def check(request: HttpRequest) -> bool:
        from dashboard.access import user_can_access_section
        return user_can_access_section(request.user, section)
    return check


# Иконки Unfold (Material Symbols) для разделов дашборда.
_DASHBOARD_ICONS = {
    "overview": "dashboard", "traffic": "language", "matches": "sports_soccer", "evaluation_sessions": "checklist",
    "data_health": "monitor_heart", "data_trust": "verified", "duplicate_players": "group", "names_review": "auto_awesome",
    "parser_tools": "dns", "users": "person", "antifraud": "gpp_maybe", "reports": "flag", "audit": "history",
    "experts": "mic", "channel": "campaign", "social_content": "share", "announcements": "campaign", "ads": "ads_click",
    "mourning": "sentiment_sad", "system_status": "monitor_heart", "server": "dns", "platform_settings": "tune",
    "scripts": "terminal", "admin_bot": "smart_toy", "access_roles": "admin_panel_settings",
}


def dashboard_sidebar(title) -> list[dict]:
    """Группы сайдбара админки — те же разделы и порядок, что в меню дашборда (dashboard/nav_data.py)."""
    from django.urls import reverse_lazy
    from django.utils.text import format_lazy

    from dashboard.nav_data import NAV_GROUPS

    return [{
        "title": format_lazy("{} · {}", title, group.label),
        "separator": i == 0,
        "collapsible": True,
        "items": [{"title": item.label, "icon": _DASHBOARD_ICONS.get(item.key, "chevron_right"),
                   "link": reverse_lazy(item.url_name), "permission": dashboard_perm(item.key)} for item in group.items],
    } for i, group in enumerate(NAV_GROUPS)]
