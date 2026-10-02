# adminbot/views.py
"""Эндпоинт ретранслятора для агента и страница «Telegram-бот» в дашборде."""
from __future__ import annotations

from django.conf import settings
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from core.utils import get_client_ip, is_rate_limited

from . import relay
from . import telegram as tg
from .models import BotLink, BotLinkCode


@never_cache
@require_GET
def relay_view(request):
    """GET /bot/relay/?env=dev — агент забирает свои апдейты (долгий опрос). ping=1 — только статус."""
    if is_rate_limited(f"adminbot:relay:{get_client_ip(request)}", 120, 60):
        return HttpResponseForbidden()
    if not relay.secret_ok(request.headers.get("X-Relay-Key")):
        return HttpResponseForbidden()
    env = request.GET.get("env", "")
    if env not in ("dev", "prod") or env == settings.ADMIN_BOT_ENV:
        return JsonResponse({"error": "bad env"}, status=400)
    status = {"listener": relay.listener_env(), "hub_env": settings.ADMIN_BOT_ENV}
    if request.GET.get("ping"):
        return JsonResponse(status)
    relay.mark_agent(env)
    updates = relay.pop_all(env) if status["listener"] else []
    relay.mark_agent(env)
    return JsonResponse({**status, "updates": updates})


def _bot_status() -> dict:
    return {
        "enabled": tg.enabled(),
        "env": settings.ADMIN_BOT_ENV,
        "listener": relay.listener_env(),
        "other_online": relay.agent_online("dev" if settings.ADMIN_BOT_ENV == "prod" else "prod"),
        "hub": settings.ADMIN_BOT_HUB_URL,
        "relay_secret": bool(settings.ADMIN_BOT_RELAY_SECRET),
    }


@staff_member_required
def dashboard_page(request):
    code = None
    if request.method == "POST" and request.POST.get("action") == "code":
        BotLinkCode.objects.filter(user=request.user, used_at__isnull=True).delete()
        code = BotLinkCode.objects.create(user=request.user)
    links = BotLink.objects.select_related("user").order_by("-linked_at") if request.user.is_superuser \
        else BotLink.objects.filter(user=request.user)
    return render(request, "dashboard/admin_bot.html", {
        "page_title": "Telegram-бот — DOPX Staff",
        "active_tab": "admin_bot",
        "status": _bot_status(),
        "code": code,
        "links": links,
        "my_link": BotLink.objects.filter(user=request.user).first(),
    })


@staff_member_required
@require_POST
def dashboard_unlink(request):
    """Отвязать себя; суперпользователь — любого или всех (аварийный выключатель)."""
    from dashboard.audit import log_staff_action
    from dashboard.models import AuditAction

    target = request.POST.get("link", "")
    qs = BotLink.objects.all() if request.user.is_superuser else BotLink.objects.filter(user=request.user)
    if target != "all":
        qs = qs.filter(pk=target)
    elif not request.user.is_superuser:
        return HttpResponseForbidden()
    n = qs.count()
    qs.delete()
    log_staff_action(request, AuditAction.BOT_ACTION, target="Отвязка Telegram", details={"removed": n, "scope": target})
    messages.success(request, f"Отвязано аккаунтов: {n}.")
    return redirect("dashboard:admin_bot")
