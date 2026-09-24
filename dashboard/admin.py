# dashboard/admin.py
from __future__ import annotations

from django.contrib import admin
from unfold.admin import ModelAdmin

from core.admin_actions import export_as_csv
from dashboard.models import StaffAccessGrant, StaffActionLog


@admin.register(StaffActionLog)
class StaffActionLogAdmin(ModelAdmin):
    """Аудит-лог — только чтение."""

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


@admin.register(StaffAccessGrant)
class StaffAccessGrantAdmin(ModelAdmin):
    """Роли доступа в /admin/ — только для суперпользователя."""

    list_display = ("user", "section_count", "updated_by", "updated_at")
    search_fields = ("user__username",)
    autocomplete_fields = ("user",)
    readonly_fields = ("updated_at",)

    def section_count(self, obj):
        return len(obj.allowed_sections)
    section_count.short_description = "Разделов разрешено"
