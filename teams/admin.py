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

    # НОВОЕ: один раз проставить пары дерби-соперников для бейджа
    # "derby_hunter" (users/badges.py) — удобный виджет "выбрать несколько
    # из списка" вместо голого multiple-select.
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