# dashboard/middleware.py
"""
Принудительная 2FA для staff-доступа (продуктовый апгрейд, "защита на
высшем уровне") — django-otp сам по себе НИЧЕГО не блокирует, он только
прикрепляет `request.user.is_verified()` к запросу (см. OTPMiddleware в
MIDDLEWARE, dopx/settings.py). Фактическое принудительное требование
пройти OTP-проверку — это наш код, здесь.

Сознательно НЕ используем `django_otp.admin.OTPAdminSite` (стандартный
рецепт django-otp для admin) — он бы защищал ТОЛЬКО /admin/, а нам нужна
единая защита и для /admin/, и для /staff/dashboard/ одним и тем же
механизмом (сотрудник логинится один раз, проходит OTP один раз, дальше
у него есть доступ в обе панели в рамках одной сессии).
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.shortcuts import redirect
from django.urls import reverse
from django_otp import devices_for_user

logger = logging.getLogger("django.security")

# Пути, которые ДОЛЖНЫ оставаться доступны БЕЗ пройденной OTP-проверки —
# иначе сотрудник, ещё не прошедший challenge, не сможет даже дойти до
# страницы, где эту проверку проходят (классический lockout-баг).
EXEMPT_PATH_PREFIXES = (
    "/staff/dashboard/security/",
)


class StaffTwoFactorEnforcementMiddleware:
    """
    Для каждого staff-запроса к /admin/ или /staff/dashboard/ (кроме
    вьюх самой 2FA-подсистемы):
      1. Если STAFF_2FA_ENFORCED=False (аварийный рубильник) — пропускаем.
      2. Если запрос не от аутентифицированного staff — пропускаем
         (авторизацией дальше по цепочке занимается staff_member_required
         / admin login, это не забота этой мидлвари).
      3. Если у пользователя уже пройдена OTP-проверка в ЭТОЙ сессии
         (request.user.is_verified(), выставляется OTPMiddleware) — пропускаем.
      4. Если у пользователя есть подтверждённое TOTP-устройство — редирект
         на страницу ввода кода (challenge).
      5. Иначе — у пользователя ВООБЩЕ нет настроенной 2FA — принудительный
         редирект на страницу первичной настройки (setup), обхода нет.
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
        if not (path.startswith("/admin/") or path.startswith("/staff/dashboard/")):
            return False
        if any(path.startswith(prefix) for prefix in EXEMPT_PATH_PREFIXES):
            return False
        # /admin/login/ и /admin/logout/ должны быть доступны без OTP —
        # иначе незалогиненный пользователь не может даже дойти до формы
        # входа (пароль ещё не введён, откуда взяться OTP-сессии).
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

        # confirmed=True (по умолчанию в django_otp) — устройства, ещё не
        # прошедшие первичное подтверждение кодом, не считаются: пользователь
        # с "недоделанным" TOTP-устройством должен попасть на setup заново,
        # а не зависнуть в challenge с устройством, для которого он никогда
        # не подтверждал секрет.
        confirmed_devices = list(devices_for_user(user, confirmed=True))
        has_confirmed_device = bool(confirmed_devices)
        target = (
            reverse("dashboard:two_factor_challenge")
            if has_confirmed_device
            else reverse("dashboard:two_factor_setup")
        )

        # Диагностический лог — временно, чтобы поймать баг, из-за которого
        # staff однажды увидел challenge вместо setup при пустой базе
        # устройств. Убрать после подтверждения, что решение маршрутизации
        # стабильно совпадает с реальным состоянием БД.
        logger.warning(
            f"2FA ROUTING: user={user.username} path={request.path} "
            f"is_verified={user.is_verified()} confirmed_devices={confirmed_devices} "
            f"has_confirmed_device={has_confirmed_device} -> target={target}"
        )

        if request.path == target:
            return None

        next_param = f"?next={request.get_full_path()}"
        return redirect(f"{target}{next_param}")
