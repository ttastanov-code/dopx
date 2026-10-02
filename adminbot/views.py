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

from . import debug, relay, writer
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


def _debug_action(request, action: str) -> None:
    """Кнопки отладки (только суперпользователь)."""
    from django.core.cache import cache

    from dashboard.audit import log_staff_action
    from dashboard.models import AuditAction

    from . import debug

    if action == "test_message":
        link = BotLink.objects.filter(user=request.user).first()
        if not link:
            messages.error(request, "Ваш аккаунт не привязан к боту.")
            return
        ok = tg.send(link.telegram_id, f"🧪 Тестовое сообщение из дашборда ({settings.ADMIN_BOT_ENV}). Бот может вам писать.")
        (messages.success if ok else messages.error)(request, "Сообщение отправлено." if ok else "Telegram не принял сообщение — см. журнал ниже.")
    elif action == "check_channel":
        cache.delete("adminbot:channel_status")
        messages.info(request, "Статус канала обновлён.")
    elif action == "delete_webhook":
        try:
            tg.call("deleteWebhook")
            messages.success(request, "Вебхук снят — бот снова получает сообщения опросом.")
        except Exception as e:
            messages.error(request, f"Не получилось: {e}")
    elif action == "clear_log":
        debug.clear_log()
        messages.success(request, "Журнал очищен.")
    elif action == "ai_reset":
        from core import llm
        for provider, _label in llm.PROVIDERS:
            cache.delete(writer._breaker(provider))
        messages.success(request, "Снова пробую писать тексты через ИИ.")
    elif action == "reset_offset":
        cache.delete("adminbot:offset")
        messages.success(request, "Позиция чтения сброшена: бот заново заберёт непрочитанные апдейты.")
    log_staff_action(request, AuditAction.BOT_ACTION, target=f"Отладка бота: {action}", details={})


def _tg_info() -> dict:
    from django.core.cache import cache

    info = cache.get("adminbot:tg_info")
    if info is None:
        info = debug.telegram_info()
        cache.set("adminbot:tg_info", info, 60)
    return info


def _debug_context() -> dict:
    from django.core.cache import cache

    from . import channel
    from .models import ChannelPost, Incident

    env = settings.ADMIN_BOT_ENV
    return {
        "hb": debug.heartbeat(),
        "tg": _tg_info(),
        "log": debug.recent_log()[:40],
        "incidents": Incident.objects.filter(resolved_at__isnull=True).select_related("acked_by")[:10],
        "channel_queue": ChannelPost.objects.filter(status__in=["draft", "scheduled", "failed"]).count(),
        "channel_failed": ChannelPost.objects.filter(status="failed").count(),
        "channel_ready": channel.configured(),
        "watch_scripts": len(cache.get(f"adminbot:watch:{env}") or []),
        "watch_deploys": len(cache.get(f"adminbot:deploy_watch:{env}") or []),
        "ai": writer.status(),
    }


@staff_member_required
def dashboard_page(request):
    code = None
    if request.method == "POST" and request.POST.get("action") == "code":
        BotLinkCode.objects.filter(user=request.user, used_at__isnull=True).delete()
        code = BotLinkCode.objects.create(user=request.user)
    elif request.method == "POST" and request.user.is_superuser and request.POST.get("action", "").startswith("dbg_"):
        _debug_action(request, request.POST["action"][4:])
        return redirect("dashboard:admin_bot")
    links = BotLink.objects.select_related("user").order_by("-linked_at") if request.user.is_superuser \
        else BotLink.objects.filter(user=request.user)
    return render(request, "dashboard/admin_bot.html", {
        "page_title": "Telegram-бот — DOPX Staff",
        "active_tab": "admin_bot",
        "status": _bot_status(),
        "code": code,
        "links": links,
        "my_link": BotLink.objects.filter(user=request.user).first(),
        "bot_alive": bool((debug.heartbeat() or {}).get("alive")),
        "bot_username": _tg_info().get("username", ""),
        **(_debug_context() if request.user.is_superuser else {}),
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


# ---------------- Раздел «Telegram-канал»
MAX_IMAGE = 10 * 1024 * 1024


def _channel_status() -> dict:
    from django.core.cache import cache

    from . import channel

    if not channel.configured():
        return {"configured": False}
    status = cache.get("adminbot:channel_status")
    if status is None:
        status = tg.channel_status(channel.channel_id())
        cache.set("adminbot:channel_status", status, 300)
    return {"configured": True, "id": channel.channel_id(), **status}


def _image(request):
    upload = request.FILES.get("image")
    if not upload:
        return None, ""
    if upload.size > MAX_IMAGE or not (upload.content_type or "").startswith("image/"):
        return None, "Картинка — JPG/PNG до 10 МБ."
    return upload, ""


def _audit(request, post, what: str):
    from dashboard.audit import log_staff_action
    from dashboard.models import AuditAction

    log_staff_action(request, AuditAction.CHANNEL_POST, target=f"{what}: {post.text[:80]}", details={"post_id": post.pk})


def _apply_action(request, post, action: str) -> None:
    """Сохранить / опубликовать / запланировать — общий хвост формы создания и правки."""
    from datetime import datetime

    from django.utils import timezone

    from . import channel

    if action == "publish":
        ok, message = channel.publish(post, request.user)
        (messages.success if ok else messages.error)(request, message)
        if ok:
            _audit(request, post, "Опубликован")
        return
    if action == "schedule":
        try:
            when = timezone.make_aware(datetime.fromisoformat(request.POST.get("scheduled_at", "")))
        except ValueError:
            messages.error(request, "Укажите дату и время публикации.")
            return
        post.status, post.scheduled_at = "scheduled", when
        post.save(update_fields=["status", "scheduled_at", "updated_at"])
        messages.success(request, f"Запланирован на {timezone.localtime(when):%d.%m %H:%M}.")
        _audit(request, post, "Запланирован")
        return
    if post.status == "scheduled" and action == "draft":
        post.status, post.scheduled_at = "draft", None
        post.save(update_fields=["status", "scheduled_at", "updated_at"])
    messages.success(request, "Сохранено.")


@staff_member_required
def channel_page(request):
    from django.core.paginator import Paginator

    from . import channel
    from .models import ChannelConfig, ChannelPost

    if request.method == "POST" and request.POST.get("action") == "samples":
        from dashboard.audit import log_staff_action
        from dashboard.models import AuditAction

        from .tasks import make_samples

        link = BotLink.objects.filter(user=request.user).first()
        make_samples.delay(link.telegram_id if link else None)
        log_staff_action(request, AuditAction.CHANNEL_POST, target="Пробные посты", details={})
        messages.success(request, "Готовлю пробные черновики всех форматов — через минуту-две обновите страницу, они появятся в очереди.")
        return redirect("dashboard:channel")
    if request.method == "POST":
        text = channel.clean_html(request.POST.get("text", "")).strip()
        upload, error = _image(request)
        if error or not text:
            messages.error(request, error or "Напишите текст поста.")
            return redirect("dashboard:channel")
        post = ChannelPost.objects.create(kind="manual", text=text[:4000], buttons=channel.parse_buttons(request.POST.get("buttons", "")),
                                          image=channel.save_upload(upload) if upload else "", created_by=request.user)
        _audit(request, post, "Создан")
        _apply_action(request, post, request.POST.get("action", "draft"))
        return redirect("dashboard:channel")

    queue = ChannelPost.objects.filter(status__in=["draft", "scheduled", "failed"]).order_by("status", "scheduled_at", "-created_at")
    published = Paginator(ChannelPost.objects.filter(status="published").order_by("-published_at"), 20).get_page(request.GET.get("page"))
    cfg = ChannelConfig.get()
    labels = dict(ChannelPost.KIND_CHOICES)
    return render(request, "dashboard/channel.html", {
        "page_title": "Telegram-канал — DOPX Staff",
        "active_tab": "channel",
        "status": _channel_status(),
        "queue": queue,
        "published": published,
        "modes": [(k, labels[k], hint, cfg.mode(k)) for k, hint in channel.KIND_HINTS.items()],
        "mode_choices": channel.MODES,
        "quiet_hours": cfg.quiet_hours,
    })


@staff_member_required
def channel_post(request, post_id: int):
    from django.shortcuts import get_object_or_404

    from . import channel
    from .models import ChannelPost

    post = get_object_or_404(ChannelPost, pk=post_id)
    if request.method == "POST":
        if post.status not in ("draft", "scheduled", "failed"):
            messages.error(request, "Опубликованный пост меняйте в самом канале.")
            return redirect("dashboard:channel")
        upload, error = _image(request)
        if error:
            messages.error(request, error)
            return redirect("dashboard:channel_post", post_id=post.pk)
        post.text = channel.clean_html(request.POST.get("text", "")).strip()[:4000] or post.text
        post.buttons = channel.parse_buttons(request.POST.get("buttons", ""))
        if upload:
            post.image, post.tg_file_id = channel.save_upload(upload), ""
        elif request.POST.get("drop_image"):
            post.image, post.tg_file_id = "", ""
        post.save()
        _apply_action(request, post, request.POST.get("action", "draft"))
        return redirect("dashboard:channel")
    from django.core.files.storage import default_storage

    return render(request, "dashboard/channel_post.html", {
        "page_title": "Пост в канал — DOPX Staff",
        "active_tab": "channel",
        "post": post,
        "image_url": default_storage.url(post.image) if post.image else "",
        "buttons_text": "\n".join(f"{b[0]} | {b[1]}" for b in post.buttons if len(b) == 2),
        "editable": post.status in ("draft", "scheduled", "failed"),
    })


@staff_member_required
@require_POST
def channel_post_delete(request, post_id: int):
    from . import channel
    from .models import ChannelPost

    post = ChannelPost.objects.filter(pk=post_id).first()
    if post and post.status == "published":
        ok, message = channel.unpublish(post)
        (messages.success if ok else messages.error)(request, message)
        if ok:
            _audit(request, post, "Удалён из канала")
        return redirect("dashboard:channel")
    if post:
        post.status = "cancelled"
        post.save(update_fields=["status", "updated_at"])
        _audit(request, post, "Удалён")
        messages.success(request, "Пост удалён из очереди.")
    return redirect("dashboard:channel")


@staff_member_required
@require_POST
def channel_modes(request):
    from dashboard.audit import log_staff_action
    from dashboard.models import AuditAction

    from . import channel
    from .models import ChannelConfig

    cfg = ChannelConfig.get()
    cfg.modes = {k: request.POST.get(f"mode_{k}") for k in channel.KIND_HINTS if request.POST.get(f"mode_{k}") in dict(channel.MODES)}
    cfg.quiet_hours = request.POST.get("quiet_hours") == "on"
    cfg.save()
    log_staff_action(request, AuditAction.CHANNEL_POST, target="Режимы автопостинга", details={"modes": cfg.modes, "quiet": cfg.quiet_hours})
    messages.success(request, "Режимы автопостинга сохранены.")
    return redirect("dashboard:channel")
