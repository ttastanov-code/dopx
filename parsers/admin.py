# parsers/admin.py
from django.contrib import admin
from unfold.admin import ModelAdmin

from core.admin_actions import export_as_csv

from .models import ParserSyncRun


@admin.register(ParserSyncRun)
class ParserSyncRunAdmin(ModelAdmin):
    """Только чтение — записи создаются исключительно из
    `parsers/tasks.py::update_match_statuses`, руками их редактировать
    незачем (см. dashboard/services.py::data_health_summary для основного
    UI поверх этих данных — эта admin-страница нужна как fallback/для
    отладки конкретного запуска)."""

    list_display = ('task_name', 'created_at', 'total', 'updated', 'errors', 'new_events', 'error_rate_percent')
    list_filter = ('task_name', 'created_at')
    ordering = ('-created_at',)
    readonly_fields = [f.name for f in ParserSyncRun._meta.fields] + ['duration_seconds', 'error_rate_percent']
    actions = [export_as_csv]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def error_rate_percent(self, obj):
        return f"{obj.error_rate_percent}%"
    error_rate_percent.short_description = 'Ошибок, %'
