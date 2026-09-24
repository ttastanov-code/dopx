# core/admin.py
from django.contrib import admin
from unfold.admin import ModelAdmin

from core.admin_mixins import SuperuserOnlyAdminMixin
from core.models import PlatformSetting


@admin.register(PlatformSetting)
class PlatformSettingAdmin(SuperuserOnlyAdminMixin, ModelAdmin):
    """Настройки платформы в /admin/. Правка отсюда не сбрасывает кэш —
    применится в течение 60 с.
    """

    list_display = ("key", "value", "value_type", "updated_at", "updated_by")
    list_filter = ("value_type",)
    search_fields = ("key", "description")
    readonly_fields = ("updated_at",)
    ordering = ("key",)
