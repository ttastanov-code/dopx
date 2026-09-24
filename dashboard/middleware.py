# dashboard/middleware.py
"""Принудительная 2FA для staff на /admin/, /staff/dashboard/ и схеме/доках API
+ проверка доступа к разделам дашборда.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.shortcuts import redirect, render
from django.urls import reverse
from django_otp import devices_for_user

from .access import resolve_section_for_path, user_can_access_section

logger = logging.getLogger("django.security")

# Пути без OTP — страницы самой 2FA.
EXEMPT_PATH_PREFIXES = (
    "/staff/dashboard/security/",
)

# Схема и доки API тоже требуют 2FA.
ENFORCED_PATH_PREFIXES = (
    "/admin/",
    "/staff/dashboard/",
    "/api/schema/",
    "/api/docs/",
    "/api/redoc/",
)


class StaffTwoFactorEnforcementMiddleware:
    """Для staff на защищённых путях:
    1. STAFF_2FA_ENFORCED=False — пропускаем;
    2. не staff — пропускаем;
    3. OTP уже пройден — пропускаем;
    4. есть устройство — на challenge;
    5. нет устройства — на setup.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if self._requires_check(request):
            response = self._enforce(request)
            if response is not None:
                return response
        return self.get_response(request)

    def _requires_check(self, request) -> bool:
        if not getattr(settings, "STAFF_2FA_ENFORCED", True):
            return False
        path = request.path
        if not path.startswith(ENFORCED_PATH_PREFIXES):
            return False
        if any(path.startswith(prefix) for prefix in EXEMPT_PATH_PREFIXES):
            return False
        # Вход/выход в админку — без OTP.
        if path in (reverse("admin:login"), reverse("admin:logout")):
            return False
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated or not user.is_staff:
            return False
        return True

    def _enforce(self, request):
        user = request.user
        if user.is_verified():
            return None

        # Только подтверждённые устройства.
        confirmed_devices = list(devices_for_user(user, confirmed=True))
        has_confirmed_device = bool(confirmed_devices)
        target = (
            reverse("dashboard:two_factor_challenge")
            if has_confirmed_device
            else reverse("dashboard:two_factor_setup")
        )

        # Временный диагностический лог маршрутизации challenge/setup.
        logger.warning(
            f"2FA ROUTING: user={user.username} path={request.path} "
            f"is_verified={user.is_verified()} confirmed_devices={confirmed_devices} "
            f"has_confirmed_device={has_confirmed_device} -> target={target}"
        )

        if request.path == target:
            return None

        next_param = f"?next={request.get_full_path()}"
        return redirect(f"{target}{next_param}")


class DashboardSectionAccessMiddleware:
    """Доступ к разделам /staff/dashboard/ по StaffAccessGrant.
    Стоит после 2FA-мидлвари. /admin/ не касается.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if self._requires_check(request):
            response = self._enforce(request)
            if response is not None:
                return response
        return self.get_response(request)

    def _requires_check(self, request) -> bool:
        if not request.path.startswith("/staff/dashboard/"):
            return False
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated or not user.is_staff:
            return False
        return True

    def _enforce(self, request):
        section = resolve_section_for_path(request.path)
        if user_can_access_section(request.user, section):
            return None
        logger.warning(
            f"DASHBOARD ACCESS DENIED: user={request.user.username} "
            f"path={request.path} section={section}"
        )
        return render(
            request, "dashboard/access_denied.html",
            {"section_label": section, "page_title": "Доступ запрещён — DOPX Staff"},
            status=403,
        )
