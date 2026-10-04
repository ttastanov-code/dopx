# dashboard/views/settings.py
"""Настройки платформы и флаги."""
from __future__ import annotations

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from core.models import PlatformSetting

from ..audit import log_staff_action
from ..models import AuditAction


# ============================================================
# Настройки платформы (PlatformSetting). Секреты сюда не кладём.
# ============================================================

@staff_member_required
def platform_settings(request):
    from adminbot import flags, writer

    context = {
        "page_title": "Настройки платформы — DOPX Staff",
        "active_tab": "platform_settings",
        # Переключатели «ИИ и оповещения» — своим блоком, в общей таблице не дублируем.
        "settings_list": PlatformSetting.objects.select_related("updated_by").exclude(key__in=flags.FLAGS).order_by("key"),
        "type_choices": PlatformSetting.TYPE_CHOICES,
        "flags": flags.overview(),
        "ai": writer.status(),
    }
    return render(request, "dashboard/platform_settings.html", context)


@staff_member_required
@require_POST
def platform_flags_save(request):
    """Блок «ИИ и оповещения»: действует сразу, без перезапуска."""
    from adminbot import flags

    changed = {}
    for row in flags.overview():
        if row["kind"] == "bool":
            new = request.POST.get(row["key"]) == "on"
        else:
            new = request.POST.get(row["key"], row["value"])
            if new not in dict(row["options"]):
                continue
        if new != row["value"] or row["from_env"]:
            flags.set_flag(row["key"], new, request.user)
            changed[row["key"]] = new
    log_staff_action(request, AuditAction.PLATFORM_SETTING_CHANGED, target="ИИ и оповещения", details={"changed": changed})
    messages.success(request, "Сохранено — действует сразу.")
    return redirect("dashboard:platform_settings")


@staff_member_required
@require_POST
def platform_settings_create(request):
    key = request.POST.get("key", "").strip()
    if not key:
        messages.error(request, "Ключ не может быть пустым")
        return redirect("dashboard:platform_settings")
    if PlatformSetting.objects.filter(key=key).exists():
        messages.error(request, f"Настройка «{key}» уже существует")
        return redirect("dashboard:platform_settings")

    value_type = request.POST.get("value_type", PlatformSetting.TYPE_STRING)
    if value_type not in dict(PlatformSetting.TYPE_CHOICES):
        value_type = PlatformSetting.TYPE_STRING

    setting = PlatformSetting.objects.create(
        key=key,
        value=request.POST.get("value", "").strip(),
        value_type=value_type,
        description=request.POST.get("description", "").strip(),
        updated_by=request.user,
    )
    from django.core.cache import cache
    cache.delete(f"platform_setting:{key}")

    messages.success(request, f"Настройка «{key}» создана")
    log_staff_action(
        request, AuditAction.PLATFORM_SETTING_CREATED,
        target=key, details={"value": setting.value, "value_type": setting.value_type},
    )
    return redirect("dashboard:platform_settings")


@staff_member_required
@require_POST
def platform_settings_update(request, key):
    setting = get_object_or_404(PlatformSetting, key=key)
    before_value = setting.value

    value_type = request.POST.get("value_type", setting.value_type)
    if value_type in dict(PlatformSetting.TYPE_CHOICES):
        setting.value_type = value_type
    setting.value = request.POST.get("value", "").strip()
    setting.description = request.POST.get("description", "").strip()
    setting.updated_by = request.user
    setting.save(update_fields=["value", "value_type", "description", "updated_by", "updated_at"])

    from django.core.cache import cache
    cache.delete(f"platform_setting:{key}")

    messages.success(request, f"«{key}» обновлена — новое значение применится в течение минуты (кэш)")
    log_staff_action(
        request, AuditAction.PLATFORM_SETTING_CHANGED,
        target=key, details={"before": before_value, "after": setting.value, "value_type": setting.value_type},
    )
    return redirect("dashboard:platform_settings")


@staff_member_required
@require_POST
def platform_settings_delete(request, key):
    setting = get_object_or_404(PlatformSetting, key=key)
    setting.delete()

    from django.core.cache import cache
    cache.delete(f"platform_setting:{key}")

    messages.success(request, f"«{key}» удалена")
    log_staff_action(request, AuditAction.PLATFORM_SETTING_DELETED, target=key)
    return redirect("dashboard:platform_settings")
