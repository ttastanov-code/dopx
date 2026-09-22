# parsers/admin.py
from django.contrib import admin
from django.utils import timezone
from unfold.admin import ModelAdmin

from core.admin_actions import export_as_csv

from .models import ConfirmedNameCorrection, NameVerificationSuggestion, ParserDiscrepancy, ParserSyncRun


@admin.register(ParserSyncRun)
class ParserSyncRunAdmin(ModelAdmin):
    """Только чтение — записи пишет `parsers/sportmonks/tasks.py::
    _record_sync_run` (source='sportmonks'). Исторические строки с
    source='kff' писала `parsers/tasks.py::update_match_statuses` до
    2026-09-09, когда KFF-парсер был физически удалён — писать этот
    source больше некому, но старые строки в БД остаются как есть.
    Руками редактировать незачем (см. dashboard/services.py::
    data_health_summary для основного UI поверх этих данных — эта
    admin-страница нужна как fallback/для отладки конкретного запуска)."""

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


@admin.register(NameVerificationSuggestion)
class NameVerificationSuggestionAdmin(ModelAdmin):
    """Основной рабочий интерфейс — очередь «Проверка ФИО (ИИ)» на staff-
    дашборде (dashboard/views.py::names_review), где approve/reject сразу
    пишет ConfirmedNameCorrection и обновляет сущность. Эта admin-страница —
    только для отладки/просмотра сырых данных, без кастомных экшенов."""

    list_display = ('entity_label', 'current_first_name', 'current_last_name', 'suggested_first_name', 'suggested_last_name', 'confidence', 'status', 'created_at')
    list_filter = ('status', 'entity_label', 'confidence', 'created_at')
    readonly_fields = [f.name for f in NameVerificationSuggestion._meta.fields]

    def has_add_permission(self, request):
        return False


@admin.register(ConfirmedNameCorrection)
class ConfirmedNameCorrectionAdmin(ModelAdmin):
    """DB-версия PLAYER_NAME_CORRECTIONS (parsers/sportmonks/
    name_translations.py) — обычно создаётся через approve в дашборде, но
    ручное добавление/правку здесь не запрещаем (в отличие от
    NameVerificationSuggestion выше) — иногда проще один раз вписать
    известную поправку напрямую, чем гонять её через ИИ-проверку."""

    list_display = ('wrong_text', 'correct_text', 'source_suggestion', 'created_by', 'created_at')
    search_fields = ('wrong_text', 'correct_text')
