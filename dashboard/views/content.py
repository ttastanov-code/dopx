# dashboard/views/content.py
"""Режим траура и контент для соцсетей."""
from __future__ import annotations

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import redirect, render
from django.utils import timezone

from ..audit import log_staff_action
from ..models import AuditAction


@staff_member_required
def mourning_mode(request):
    """Режим траура: включение сейчас или по периоду, текст плашки, что приглушить."""
    from datetime import datetime

    from core.live import bump_data_version
    from core.models import MourningMode
    from core.mourning import current_mourning, forget

    obj, _created = MourningMode.objects.get_or_create(pk=1)

    def parse_dt(value):
        if not value:
            return None
        try:
            return timezone.make_aware(datetime.fromisoformat(value))
        except ValueError:
            return None

    if request.method == "POST":
        action = request.POST.get("action", "save")
        before = {"is_enabled": obj.is_enabled, "message": obj.message}
        if action == "disable":
            obj.is_enabled = False
        else:
            obj.is_enabled = action == "enable" or request.POST.get("is_enabled") == "on"
            obj.message = (request.POST.get("message") or "").strip()[:255] or MourningMode.DEFAULT_MESSAGE
            obj.starts_at = parse_dt(request.POST.get("starts_at"))
            obj.ends_at = parse_dt(request.POST.get("ends_at"))
            obj.grayscale = request.POST.get("grayscale") == "on"
            obj.hide_ads = request.POST.get("hide_ads") == "on"
            obj.mute_push = request.POST.get("mute_push") == "on"
        if obj.starts_at and obj.ends_at and obj.ends_at <= obj.starts_at:
            messages.error(request, "Окончание должно быть позже начала.")
            return redirect("dashboard:mourning")
        obj.updated_by = request.user
        obj.save()
        forget()
        bump_data_version()  # открытые вкладки подхватят сразу
        log_staff_action(
            request, AuditAction.MOURNING_CHANGED, target=obj.message,
            details={
                "before": before, "is_enabled": obj.is_enabled,
                "starts_at": obj.starts_at.isoformat() if obj.starts_at else None,
                "ends_at": obj.ends_at.isoformat() if obj.ends_at else None,
                "grayscale": obj.grayscale, "hide_ads": obj.hide_ads, "mute_push": obj.mute_push,
            },
        )
        messages.success(request, "Режим траура выключен." if not obj.is_enabled else "Настройки режима траура сохранены.")
        return redirect("dashboard:mourning")

    now = timezone.now()
    if obj.is_enabled and obj.starts_at and obj.starts_at > now:
        state = "scheduled"
    elif current_mourning():
        state = "active"
    elif obj.is_enabled and obj.ends_at and obj.ends_at <= now:
        state = "finished"
    else:
        state = "off"
    context = {
        "page_title": "Режим траура — DOPX Staff",
        "active_tab": "mourning",
        "mourning_obj": obj,
        "mourning_state": state,
    }
    return render(request, "dashboard/mourning.html", context)


@staff_member_required
def social_content(request):
    """Контент для соцсетей по итогам тура: картинки и готовые подписи."""
    from engagement.social import weekly_content

    force = request.method == "POST"
    data = weekly_content(force=force)
    if force:
        messages.success(request, "Картинки пересобраны.")
        return redirect("dashboard:social_content")
    return render(request, "dashboard/social_content.html", {"active_tab": "social_content", "content": data})
