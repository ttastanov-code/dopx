from django.contrib import admin
from unfold.admin import ModelAdmin

from core.admin_actions import export_as_csv

from .models import Player


@admin.register(Player)
class PlayerAdmin(ModelAdmin):

    list_display = (
        "first_name",
        "last_name",
        "team",
        "position",
        "number",
        "is_active",
    )

    search_fields = (
        "first_name",
        "last_name",
    )

    list_filter = (
        "team",
        "position",
        "is_active",
    )

    autocomplete_fields = ("team",)

    actions = [export_as_csv]