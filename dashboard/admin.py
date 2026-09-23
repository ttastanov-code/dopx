# dashboard/admin.py
from __future__ import annotations

from django.contrib import admin
from unfold.admin import ModelAdmin

from core.admin_actions import export_as_csv
from dashboard.models import StaffAccessGrant, StaffActionLog


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


@admin.register(StaffAccessGrant)
class StaffAccessGrantAdmin(ModelAdmin):
    """2026-09-23, прямая просьба пользователя — виден и в Django /admin/,
    не только на /staff/dashboard/access/. Раздел доступа сам по себе
    остаётся суперпользовательским: и эта admin-страница, и dashboard-
    страница видны/доступны только is_superuser (Django admin сам по себе
    показывает разделы по правам Django-permissions, а не по нашему
    StaffAccessGrant — эта модель никак не ограничивает саму себя)."""

    list_display = ("user", "section_count", "updated_by", "updated_at")
    search_fields = ("user__username",)
    autocomplete_fields = ("user",)
    readonly_fields = ("updated_at",)

    def section_count(self, obj):
        return len(obj.allowed_sections)
    section_count.short_description = "Разделов разрешено"
