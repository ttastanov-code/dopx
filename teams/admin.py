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