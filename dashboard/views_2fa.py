# dashboard/views_2fa.py
"""
Самостоятельная настройка и проверка 2FA для staff. Сюда редиректит
`dashboard/middleware.py::StaffTwoFactorEnforcementMiddleware`, когда
staff-пользователь ещё не прошёл OTP-проверку в текущей сессии.

ВАЖНО: используем `@login_required` + ручную проверку `is_staff`, а НЕ
`@staff_member_required` — оба варианта эквивалентны по факту (оба проверяют
is_staff), но `staff_member_required` формально завязан на admin-специфичный
`login_url='admin:login'`; здесь это неважно, но ручная проверка чуть яснее
показывает, что доступ сюда НЕ требует пройденной OTP (иначе замкнутый круг:
мидлварь редиректит на эту страницу именно потому, что OTP ещё не пройдена).
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
    """Простая защита от open redirect — принимаем только относительный
    путь, начинающийся с "/" (не "//evil.com", не абсолютный URL)."""
    candidate = request.GET.get("next") or request.POST.get("next") or fallback
    if not candidate.startswith("/") or candidate.startswith("//"):
        return fallback
    return candidate


def _throttle_wait_seconds(device) -> int | None:
    """
    django-otp троттлит КАЖДОЕ устройство отдельно: после неудачной попытки
    verify_token() следующая проверка блокируется на
    throttle_factor * 2^(failure_count-1) секунд с момента последней ошибки
    — даже если новый код абсолютно верный (см. ThrottlingMixin.verify_is_allowed
    в django_otp/models.py). Без этой проверки пользователь видит то же самое
    "Неверный код", что и при опечатке, и не понимает, что дело не в коде, а
    в паузе — ловили именно это на реальном инциденте (12 неудачных попыток
    → ~34 минуты блокировки TOTP-устройства).

    Возвращает None, если устройство сейчас НЕ заблокировано, иначе — сколько
    секунд осталось ждать.
    """
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

    # Уже есть подтверждённое устройство — это не страница "добавить ещё
    # одно", а именно первичный бутстрап; повторный визит уводим на challenge.
    #
    # БАГ, КОТОРЫЙ ТУТ БЫЛ: `devices_for_user(...)` возвращает генератор.
    # `if <генератор>:` в Python — ВСЕГДА True (генератор как объект truthy
    # по умолчанию, независимо от того, есть ли в нём элементы) — проверяется
    # именно наличие ссылки на объект, а не факт, что итерация даст хоть
    # один результат. Из-за этого условие срабатывало ДАЖЕ при нуле
    # устройств в базе, и /setup/ безусловно редиректил на /verify/ — юзер
    # никогда не видел QR-код, даже настраивая 2FA впервые. Правильно —
    # материализовать генератор через list()/any() перед проверкой истинности,
    # как уже сделано в dashboard/middleware.py (там баг не воспроизводился
    # именно поэтому — там сразу использовался any()).
    if any(devices_for_user(request.user, confirmed=True)):
        return redirect("dashboard:two_factor_challenge")

    device, _created = TOTPDevice.objects.get_or_create(
        user=request.user, confirmed=False, defaults={"name": "primary"},
    )

    if request.method == "POST":
        token = request.POST.get("token", "").strip()
        wait = _throttle_wait_seconds(device)
        if wait is not None:
            # Отдельное сообщение от "неверный код" — иначе пользователь
            # правильно введённым кодом продолжает биться в закрытую дверь,
            # думая, что ошибся сам (см. _throttle_wait_seconds выше).
            messages.error(
                request,
                f"Слишком много неверных попыток подряд — устройство временно "
                f"заблокировано защитой от подбора кода (это не ошибка в коде). "
                f"Подождите {wait} сек. и попробуйте снова."
            )
        elif device.verify_token(token):
            device.confirmed = True
            device.save(update_fields=["confirmed"])

            # Backup-коды генерируются РОВНО один раз — в момент первичного
            # подтверждения устройства. Это единственный момент, когда их
            # можно показать пользователю в чистом виде (StaticToken хранит
            # значение в открытом виде в БД, но UI больше никогда его не
            # покажет повторно — см. two_factor_backup_codes ниже).
            #
            # Явная зачистка ПЕРЕД созданием: раньше здесь стоял голый
            # .create() — при повторном прохождении setup (например, старое
            # confirmed-устройство было удалено вручную из БД, а
            # StaticDevice от предыдущего захода остался) накапливались
            # ВТОРОЙ StaticDevice + его StaticToken'ы. devices_for_user()
            # в two_factor_challenge перебирает ВСЕ confirmed static-девайсы
            # сразу — значит верными считались коды из СТАРОЙ пачки, которую
            # пользователь уже не видит (UI показывает backup-коды только
            # один раз), а из НОВОЙ, только что показанной пачки — только
            # тот код, что случайно не совпал по префиксу/коллизии с ничем
            # старым. Отсюда симптом "один код почему-то всегда работает,
            # остальные — никогда": это код из старой, забытой пачки.
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
    """Единственный показ backup-кодов — сразу после подтверждения
    устройства. Читаем из сессии и СРАЗУ удаляем: обновление страницы или
    повторный визит их больше не покажет (они уже сохранены в БД как хэш
    сравнения, но сам открытый текст живёт только в этом одном ответе)."""
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
        # Перебираем TOTP И static (backup) устройства одним и тем же
        # verify_token — пользователь может ввести и 6-значный код из
        # приложения, и один из заранее сохранённых backup-кодов, форма
        # не различает их специально (меньше UI, один инпут).
        #
        # У каждого устройства СВОЙ троттлинг (см. _throttle_wait_seconds) —
        # пропускаем заблокированные, не тратя на них verify_token(), и
        # запоминаем минимальное время ожидания среди них. Если проверить
        # код в итоге было НЕКЕМ (все устройства заблокированы) — это
        # отдельная ситуация от "код неверный", и пользователю нужно сказать
        # именно про неё, а не заставлять его думать, что он ошибся сам.
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
                f"Слишком много неверных попыток подряд — все ваши 2FA-устройства "
                f"временно заблокированы защитой от подбора кода (это не значит, "
                f"что код неверный). Подождите {min_wait} сек. и попробуйте снова."
            )
        else:
            messages.error(request, "Неверный код. Можно также использовать один из backup-кодов.")

    return render(request, "dashboard/security_2fa_challenge.html", {
        "page_title": "Подтверждение входа — DOPX Staff",
        "next": next_url,
    })
