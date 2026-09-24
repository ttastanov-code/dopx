from django.contrib import admin
from unfold.admin import ModelAdmin

from core.admin_actions import export_as_csv

from .models import Season


@admin.register(Season)
class SeasonAdmin(ModelAdmin):

    list_display = (
        "league",
        "year",
        "is_active",
    )

    list_filter = (
        "league",
        "is_active",
    )

    # search_fields нужен для autocomplete в MatchAdmin.
    search_fields = ("year",)

    autocomplete_fields = ("league",)

    actions = [export_as_csv]