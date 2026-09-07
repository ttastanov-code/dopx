# parsers/admin.py
from django.contrib import admin
from django.utils import timezone
from unfold.admin import ModelAdmin

from core.admin_actions import export_as_csv

from .models import ParserDiscrepancy, ParserSyncRun


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


@admin.register(ParserDiscrepancy)
class ParserDiscrepancyAdmin(ModelAdmin):
    """
    В отличие от ParserSyncRunAdmin выше, это НЕ read-only лог — сюда
    staff заходит именно чтобы разобрать конкретную запись (см. докстринг
    parsers/models.py::ParserDiscrepancy) и отметить результат: `reviewed`
    ставится вручную (не автоматически при просмотре — открыть страницу
    списка ещё не значит разобраться, что там произошло), `note` — короткий
    вывод ("подтверждено официальным протоколом KFF" / "ложное срабатывание,
    источник на секунду отдал старые данные" и т.п.).
    """

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
        # .update() — умышленно, не по одному .save() с сигналами: здесь
        # нет побочной логики уровня "сменился статус — отправить письмо",
        # как в notifications/admin.py::ContactSubmissionAdmin (см. её
        # докстринг за примером, где .update() был бы багом) — тут просто
        # массовая простановка трёх полей.
        count = queryset.filter(reviewed=False).update(
            reviewed=True, reviewed_by=request.user, reviewed_at=timezone.now()
        )
        self.message_user(request, f"Отмечено разобранными: {count}")
    mark_reviewed.short_description = 'Отметить разобранными'
