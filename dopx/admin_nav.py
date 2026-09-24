# dopx/admin_nav.py
"""Проверки прав для пунктов сайдбара Unfold (UNFOLD["SIDEBAR"] в settings).
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
