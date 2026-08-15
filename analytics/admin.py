# analytics/admin.py
from django.contrib import admin
from unfold.admin import ModelAdmin

from analytics.models import AnalyticsEvent
from core.admin_actions import export_as_csv


@admin.register(AnalyticsEvent)
class AnalyticsEventAdmin(ModelAdmin):
    """
    Только чтение — событие пишется исключительно через
    `persist_event_task` (см. analytics/tasks.py), ручное редактирование
    задним числом исказило бы воронку.
    """
    list_display = ("event_name", "user", "anonymous_id", "url_path", "created_at")
    list_filter = ("event_name", "created_at")
    search_fields = ("user__username", "anonymous_id", "session_id", "url_path")
    date_hierarchy = "created_at"
    readonly_fields = [f.name for f in AnalyticsEvent._meta.fields]
    actions = [export_as_csv]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
