# fanbot/views.py
"""Вход через Telegram (виджет на сайте и Mini App), привязка Telegram к аккаунту в профиле."""
from __future__ import annotations

import hashlib

from django.contrib import messages
from django.core import signing
from django.core.cache import cache
from django.urls import reverse
from django.utils.http import urlencode
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.clickjacking import xframe_options_exempt
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from core.utils import get_client_ip, is_rate_limited

from . import services
from .auth import verify_login_widget, verify_webapp
from .models import TelegramAccount, TelegramLinkCode


ENTER_SALT = "fanbot.miniapp-enter"


def _safe_next(request, value: str, default: str = "/") -> str:
    return value if value and url_has_allowed_host_and_scheme(value, allowed_hosts={request.get_host()}) else default


WELCOME_SKIP_KEY = "tg_welcome_skipped"


def _after_login(request, acc, nxt: str) -> str:
    """Новый аккаунт или профиль без города/почты — сначала анкета (если не отложена в этой сессии)."""
    from users.emails import profile_complete

    if getattr(acc, "created", False) or (not profile_complete(acc.user) and not request.session.get(WELCOME_SKIP_KEY)):
        return reverse("users:complete_profile") + "?" + urlencode({"next": nxt})
    return nxt


def login_widget(request):
    """Сюда Telegram возвращает пользователя после виджета входа (подписанные GET-параметры)."""
    if is_rate_limited(f"tg_login:{get_client_ip(request)}", 30, 600):
        messages.error(request, "Слишком много попыток входа. Попробуйте через несколько минут.")
        return redirect("users:login")
    tg = verify_login_widget(request.GET.dict())
    if not tg:
        messages.error(request, "Не удалось войти через Telegram: ссылка устарела или подпись неверна.")
        return redirect("users:login")
    acc = services.login_telegram(request, tg)
    if not acc:
        messages.error(request, "Аккаунт заблокирован.")
        return redirect("users:login")
    nxt = _safe_next(request, request.GET.get("next", ""))
    if not getattr(acc, "created", False):
        messages.success(request, f"Вы вошли через Telegram как {acc.user.username}.")
    return redirect(_after_login(request, acc, nxt))


@xframe_options_exempt
def miniapp(request):
    """Точка входа Mini App: скрипт Telegram отдаёт initData, страница входит и переходит дальше."""
    return render(request, "fanbot/miniapp.html", {"page_title": "DOPX"})


@csrf_exempt
@require_POST
def miniapp_auth(request):
    """initData подписан ботом — это и есть проверка запроса (вместо CSRF)."""
    if is_rate_limited(f"tg_app:{get_client_ip(request)}", 60, 600):
        return JsonResponse({"ok": False, "error": "rate"}, status=429)
    tg = verify_webapp(request.POST.get("init_data", ""))
    if not tg:
        return JsonResponse({"ok": False, "error": "bad_signature"}, status=403)
    acc = services.login_telegram(request, tg)
    if not acc:
        return JsonResponse({"ok": False, "error": "blocked"}, status=403)
    start = tg.get("start_param") or request.POST.get("start", "")
    nxt = _after_login(request, acc, services.resolve_start(start))
    # Telegram Web держит Mini App во фрейме, где сайт не откроется, — даём ссылку входа для новой вкладки.
    enter = reverse("fanbot:miniapp_enter") + "?" + urlencode({"t": signing.dumps({"u": str(acc.user_id), "n": nxt}, salt=ENTER_SALT)})
    return JsonResponse({"ok": True, "next": nxt, "enter_url": enter})


@login_required
@require_POST
def miniapp_allow(request):
    """Пользователь разрешил боту писать (WebApp.requestWriteAccess) — уведомления могут уходить."""
    TelegramAccount.objects.filter(user=request.user).update(can_message=True, notify=True)
    return JsonResponse({"ok": True})


def miniapp_enter(request):
    """Одноразовый вход по ссылке из Mini App (Telegram Web): живёт минуту, второй раз не пускает."""
    from django.contrib.auth import login

    from users.models import User

    token = request.GET.get("t", "")
    try:
        data = signing.loads(token, salt=ENTER_SALT, max_age=60)
    except signing.BadSignature:
        messages.error(request, "Ссылка входа устарела. Откройте DOPX из Telegram ещё раз.")
        return redirect("users:login")
    if not cache.add(f"tg_enter:{hashlib.sha256(token.encode()).hexdigest()}", 1, 120):
        return redirect(_safe_next(request, data.get("n", "")))
    user = User.objects.filter(pk=data["u"], is_active=True).first()
    if not user:
        return redirect("users:login")
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    return redirect(_safe_next(request, data.get("n", "")))


@login_required
@require_POST
def link_start(request):
    """Кнопка «Привязать Telegram» в профиле: одноразовый код и переход в бота."""
    if not services.enabled() or not services.bot_username():
        messages.error(request, "Telegram пока не подключён.")
        return redirect("users:notification_settings")
    TelegramLinkCode.objects.filter(user=request.user, used_at__isnull=True).delete()
    code = TelegramLinkCode.objects.create(user=request.user)
    return redirect(f"https://t.me/{services.bot_username()}?start=link_{code.code}")


@login_required
@require_POST
def phone_start(request):
    """«Подтвердить номер через Telegram»: бот просит «Поделиться номером»; без привязки — привязка той же ссылкой."""
    if not services.enabled() or not services.bot_username():
        messages.error(request, "Telegram пока не подключён.")
        return redirect("users:profile_edit")
    if TelegramAccount.objects.filter(user=request.user).exists():
        return redirect(f"https://t.me/{services.bot_username()}?start=phone")
    TelegramLinkCode.objects.filter(user=request.user, used_at__isnull=True).delete()
    code = TelegramLinkCode.objects.create(user=request.user)
    return redirect(f"https://t.me/{services.bot_username()}?start=phone_{code.code}")


@login_required
@require_POST
def unlink(request):
    back = _safe_next(request, request.POST.get("next", ""), default=reverse("users:notification_settings"))
    # Без пароля Telegram — единственный способ входа: отвязка заперла бы аккаунт.
    if not request.user.has_usable_password():
        messages.error(request, "Сначала задайте пароль: «Забыли пароль?» на странице входа пришлёт ссылку на почту. "
                                "Иначе после отвязки войти будет нечем. Сменить Telegram можно кнопкой «Перепривязать».")
        return redirect(back)
    TelegramAccount.objects.filter(user=request.user).delete()
    messages.success(request, "Telegram отвязан. Уведомления туда больше не придут.")
    return redirect(back)


@login_required
@require_POST
def toggle_notify(request):
    acc = TelegramAccount.objects.filter(user=request.user).first()
    if acc:
        acc.notify = not acc.notify
        acc.save(update_fields=["notify"])
        messages.success(request, "Уведомления в Telegram включены." if acc.notify else "Уведомления в Telegram выключены.")
    return redirect("users:notification_settings")
