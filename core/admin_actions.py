# core/admin_actions.py
"""Общие admin-экшены.

    from core.admin_actions import export_as_csv
    actions = ["export_as_csv"]
"""
from __future__ import annotations

import csv

from django.http import HttpResponse
from django.utils import timezone


def _csv_safe(value):
    """Защита от formula injection: значения с = + - @ получают ведущий апостроф."""
    if isinstance(value, str) and value and value[0] in ("=", "+", "-", "@"):
        return "'" + value
    return value


def export_as_csv(modeladmin, request, queryset):
    """Экспорт выбранных строк в CSV."""
    model = modeladmin.model
    meta = model._meta

    # Колонки — реальные поля из list_display; иначе все поля модели.
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
