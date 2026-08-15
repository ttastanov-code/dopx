from django.contrib import admin
from unfold.admin import ModelAdmin

from core.admin_actions import export_as_csv

from .models import Coach


@admin.register(Coach)
class CoachAdmin(ModelAdmin):

    list_display = (
        "first_name",
        "last_name",
        "team",
        "is_active",
    )

    search_fields = (
        "first_name",
        "last_name",
    )

    list_filter = (
        "team",
    )

    autocomplete_fields = ("team",)

    actions = [export_as_csv]