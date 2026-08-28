# core/admin_actions.py
"""
Общие admin-экшены, переиспользуемые во ВСЕХ ModelAdmin проекта (продуктовый
апгрейд — "широкий набор функций в админке"). Единая точка вместо
copy-paste одной и той же функции экспорта в 18 разных admin.py.

Использование в любом ModelAdmin:
    from core.admin_actions import export_as_csv
    actions = ["export_as_csv"]  # или добавить к уже существующему списку

Работает с ЛЮБОЙ моделью без дополнительной настройки — берёт видимые поля
из `list_display`, если это простые имена полей модели, иначе падает
обратно на все конкретные (non-relation-reverse) поля модели.
"""
from __future__ import annotations

import csv

from django.http import HttpResponse
from django.utils import timezone


def _csv_safe(value):
    """Защита от CSV/formula injection: если открыть экспортированный файл в
    Excel/Google Sheets, строковое значение, начинающееся с `=`, `+`, `-`
    или `@`, может быть интерпретировано как формула (в т.ч. вредоносная,
    если значение пришло из пользовательского ввода — username, subject
    обращения и т.п.). Ведущий апостроф заставляет Excel/Sheets показать
    значение как обычный текст. Единая точка правды — используется и здесь,
    и в `dashboard/views.py::antifraud_export_csv`."""
    if isinstance(value, str) and value and value[0] in ("=", "+", "-", "@"):
        return "'" + value
    return value


def export_as_csv(modeladmin, request, queryset):
    """Экспорт выбранных строк списка в CSV. Регистрируется как admin-экшен
    (принимает (modeladmin, request, queryset) — стандартная сигнатура
    Django admin actions), поэтому может использоваться под любым именем
    метода в `actions = [...]` любого ModelAdmin."""
    model = modeladmin.model
    meta = model._meta

    # Предпочитаем колонки list_display (то, что staff и так видит в
    # таблице) — но берём только те, что являются РЕАЛЬНЫМИ полями модели;
    # method-колонки (например, кастомные `id_short`, `status_badge` из
    # notifications/admin.py) в CSV пропускаем — их бы пришлось вызывать
    # отдельно, а без явного мэппинга это лишний риск сломать экспорт.
    field_names = [f.name for f in meta.fields]
    display_fields = [f for f in getattr(modeladmin, "list_display", []) if f in field_names]
    export_fields = display_fields or field_names

    response = HttpResponse(content_type="text/csv")
    filename = f"{meta.app_label}_{meta.model_name}_{timezone.now():%Y%m%d_%H%M}.csv"
    response["Content-Disposition"] = f'attachment; filename="{filename}"'

    writer = csv.writer(response)
    writer.writerow([meta.get_field(f).verbose_name for f in export_fields])
    for obj in queryset:
        writer.writerow([_csv_safe(getattr(obj, f)) for f in export_fields])

    return response


export_as_csv.short_description = "Экспортировать выбранное в CSV"
