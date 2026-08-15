# dashboard/admin.py
from __future__ import annotations

from django.contrib import admin
from unfold.admin import ModelAdmin

from core.admin_actions import export_as_csv
from dashboard.models import StaffActionLog


@admin.register(StaffActionLog)
class StaffActionLogAdmin(ModelAdmin):
    """Read-only — записи создаются ТОЛЬКО через dashboard.audit.log_staff_action,
    ручное редактирование/удаление аудит-лога через admin запрещено намеренно
    (иначе аудит перестаёт быть аудитом)."""

    list_display = ("created_at", "actor_username", "action", "target", "ip_address")
    list_filter = ("action", "created_at")
    search_fields = ("actor_username", "target")
    ordering = ("-created_at",)
    readonly_fields = ("created_at", "actor", "actor_username", "action", "target", "details", "ip_address")
    date_hierarchy = "created_at"
    actions = [export_as_csv]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
