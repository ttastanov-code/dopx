from django.contrib import admin
from .models import Team


@admin.register(Team)
class TeamAdmin(admin.ModelAdmin):

    list_display = (
        "name",
        "league",
        "city",
    )

    search_fields = (
        "name",
        "city",
    )

    list_filter = (
        "league",
    )