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

    # year — единственное текстово-осмысленное поле для поиска у Season;
    # добавлено также затем, что autocomplete_fields на MatchAdmin.season
    # (matches/admin.py) ТРЕБУЕТ search_fields на целевой модели — без
    # этого Django бросает SystemCheckError при старте.
    search_fields = ("year",)

    autocomplete_fields = ("league",)

    actions = [export_as_csv]