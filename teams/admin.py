from django.contrib import admin
from .models import Team, TeamSeason


@admin.register(Team)
class TeamAdmin(admin.ModelAdmin):

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


@admin.register(TeamSeason)
class TeamSeasonAdmin(admin.ModelAdmin):

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