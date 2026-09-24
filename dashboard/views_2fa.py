# dashboard/views_2fa.py
"""Настройка и проверка 2FA для staff (сюда редиректит StaffTwoFactorEnforcementMiddleware).
@login_required + проверка is_staff — OTP здесь не требуется.
"""
from __future__ import annotations

import base64
import io

import qrcode
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django_otp import devices_for_user
from django_otp import login as otp_login
from django_otp.plugins.otp_static.models import StaticDevice, StaticToken
from django_otp.plugins.otp_totp.models import TOTPDevice


def _safe_next(request, fallback: str) -> str:
    """Только относительный путь с "/" (не "//...")."""
    candidate = request.GET.get("next") or request.POST.get("next") or fallback
    if not candidate.startswith("/") or candidate.startswith("//"):
        return fallback
    return candidate


def _throttle_wait_seconds(device) -> int | None:
    """Сколько секунд устройство ещё заблокировано троттлингом django-otp (None — не заблокировано)."""
    allowed, info = device.verify_is_allowed()
    if allowed:
        return None
    locked_until = (info or {}).get("locked_until")
    if not locked_until:
        return None
    return max(0, int((locked_until - timezone.now()).total_seconds()))


@login_required
def two_factor_setup(request):
    if not request.user.is_staff:
        return HttpResponseForbidden("Только для сотрудников")

    # Подтверждённое устройство уже есть — на challenge.
    if any(devices_for_user(request.user, confirmed=True)):
        return redirect("dashboard:two_factor_challenge")

    device, _created = TOTPDevice.objects.get_or_create(
        user=request.user, confirmed=False, defaults={"name": "primary"},
    )

    if request.method == "POST":
        token = request.POST.get("token", "").strip()
        wait = _throttle_wait_seconds(device)
        if wait is not None:
            # Отдельное сообщение про блокировку.
            messages.error(
                request,
                f"Слишком много неверных попыток подряд. Устройство временно "
                f"заблокировано защитой от подбора кода (это не ошибка в коде). "
                f"Подождите {wait} сек. и попробуйте снова."
            )
        elif device.verify_token(token):
            device.confirmed = True
            device.save(update_fields=["confirmed"])

            # Удаляем старые backup-коды перед созданием новых.
            StaticDevice.objects.filter(user=request.user).delete()
            static_device = StaticDevice.objects.create(user=request.user, name="backup", confirmed=True)
            backup_tokens = []
            for _ in range(8):
                token_value = StaticToken.random_token()
                StaticToken.objects.create(device=static_device, token=token_value)
                backup_tokens.append(token_value)

            otp_login(request, device)
            request.session["_2fa_backup_tokens_shown"] = backup_tokens
            return redirect("dashboard:two_factor_backup_codes")

        messages.error(request, "Неверный код. Проверьте время на телефоне и попробуйте снова.")

    qr_img = qrcode.make(device.config_url)
    buf = io.BytesIO()
    qr_img.save(buf, format="PNG")
    qr_data_uri = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()

    return render(request, "dashboard/security_2fa_setup.html", {
        "page_title": "Настройка 2FA — DOPX Staff",
        "qr_data_uri": qr_data_uri,
        "secret_key": device.key,
        "next": _safe_next(request, reverse("dashboard:overview")),
    })


@login_required
def two_factor_backup_codes(request):
    """Backup-коды показываются один раз: читаем из сессии и сразу удаляем."""
    if not request.user.is_staff:
        return HttpResponseForbidden("Только для сотрудников")
    tokens = request.session.pop("_2fa_backup_tokens_shown", None)
    if not tokens:
        return redirect("dashboard:overview")
    return render(request, "dashboard/security_2fa_backup_codes.html", {
        "page_title": "Backup-коды — DOPX Staff",
        "tokens": tokens,
    })


@login_required
def two_factor_challenge(request):
    if not request.user.is_staff:
        return HttpResponseForbidden("Только для сотрудников")

    next_url = _safe_next(request, reverse("dashboard:overview"))

    if request.user.is_verified():
        return redirect(next_url)

    if request.method == "POST":
        token = request.POST.get("token", "").strip()
        matched_device = None
        # Проверяем код по TOTP и backup-устройствам; заблокированные пропускаем.
        # Все заблокированы — отдельное сообщение.
        min_wait = None
        any_checked = False
        for device in devices_for_user(request.user, confirmed=True):
            wait = _throttle_wait_seconds(device)
            if wait is not None:
                if min_wait is None or wait < min_wait:
                    min_wait = wait
                continue
            any_checked = True
            if device.verify_token(token):
                matched_device = device
                break

        if matched_device:
            otp_login(request, matched_device)
            return redirect(_safe_next(request, next_url))

        if not any_checked and min_wait is not None:
            messages.error(
                request,
                f"Слишком много неверных попыток подряд. Все ваши 2FA-устройства "
                f"временно заблокированы защитой от подбора кода (это не значит, "
                f"что код неверный). Подождите {min_wait} сек. и попробуйте снова."
            )
        else:
            messages.error(request, "Неверный код. Можно также использовать один из backup-кодов.")

    return render(request, "dashboard/security_2fa_challenge.html", {
        "page_title": "Подтверждение входа — DOPX Staff",
        "next": next_url,
    })
