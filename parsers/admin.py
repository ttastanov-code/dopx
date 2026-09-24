# parsers/admin.py
from django.contrib import admin
from django.utils import timezone
from unfold.admin import ModelAdmin

from core.admin_actions import export_as_csv
from core.admin_mixins import SuperuserOnlyAdminMixin

from .models import ConfirmedNameCorrection, NameVerificationSuggestion, ParserDiscrepancy, ParserSyncRun


@admin.register(ParserSyncRun)
class ParserSyncRunAdmin(ModelAdmin):
    """Журнал синков — только чтение. Пишет _record_sync_run."""

    list_display = ('task_name', 'source', 'created_at', 'total', 'updated', 'errors', 'new_events', 'error_rate_percent')
    list_filter = ('source', 'task_name', 'created_at')
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


@admin.register(ParserDiscrepancy)
class ParserDiscrepancyAdmin(SuperuserOnlyAdminMixin, ModelAdmin):
    """Расхождения импорта: staff отмечает reviewed и пишет note."""

    list_display = ('match_label', 'field_name', 'old_value', 'new_value', 'created_at', 'reviewed')
    list_filter = ('reviewed', 'field_name', 'created_at')
    search_fields = ('match_label',)
    ordering = ('reviewed', '-created_at')
    readonly_fields = ('match', 'match_label', 'field_name', 'old_value', 'new_value', 'created_at')
    fields = (
        'match', 'match_label', 'field_name', 'old_value', 'new_value', 'created_at',
        'reviewed', 'reviewed_by', 'reviewed_at', 'note',
    )
    actions = ['mark_reviewed', export_as_csv]

    def has_add_permission(self, request):
        return False

    def mark_reviewed(self, request, queryset):
        # .update() — побочной логики нет.
        count = queryset.filter(reviewed=False).update(
            reviewed=True, reviewed_by=request.user, reviewed_at=timezone.now()
        )
        self.message_user(request, f"Отмечено разобранными: {count}")
    mark_reviewed.short_description = 'Отметить разобранными'


@admin.register(NameVerificationSuggestion)
class NameVerificationSuggestionAdmin(SuperuserOnlyAdminMixin, ModelAdmin):
    """Предложения ИИ — просмотр; рабочий интерфейс в дашборде."""

    list_display = ('entity_label', 'current_first_name', 'current_last_name', 'suggested_first_name', 'suggested_last_name', 'confidence', 'status', 'created_at')
    list_filter = ('status', 'entity_label', 'confidence', 'created_at')
    readonly_fields = [f.name for f in NameVerificationSuggestion._meta.fields]

    def has_add_permission(self, request):
        return False


@admin.register(ConfirmedNameCorrection)
class ConfirmedNameCorrectionAdmin(SuperuserOnlyAdminMixin, ModelAdmin):
    """Подтверждённые поправки имён; ручное добавление разрешено."""

    list_display = ('wrong_text', 'correct_text', 'source_suggestion', 'created_by', 'created_at')
    search_fields = ('wrong_text', 'correct_text')
