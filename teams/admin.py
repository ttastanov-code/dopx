from django.contrib import admin
from unfold.admin import ModelAdmin

from core.admin_actions import export_as_csv

from .models import Team, TeamSeason


@admin.register(Team)
class TeamAdmin(ModelAdmin):

    list_display = (
        "name",
        "city",
        "external_id",
    )

    search_fields = (
        "name",
        "city",
    )

    # Пары соперников (дерби) — filter_horizontal.
    filter_horizontal = ("rivals",)

    actions = [export_as_csv]


@admin.register(TeamSeason)
class TeamSeasonAdmin(ModelAdmin):

    list_display = (
        "team",
        "season",
    )

    list_filter = (
        "season",
    )

    search_fields = (
        "team__name",
    )

    autocomplete_fields = ("team", "season")

    actions = [export_as_csv]