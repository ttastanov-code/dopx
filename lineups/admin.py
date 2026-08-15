from django.contrib import admin
from unfold.admin import ModelAdmin, TabularInline

from core.admin_actions import export_as_csv

from .models import MatchLineup, MatchLineupPlayer


class LineupPlayerInline(TabularInline):
    model = MatchLineupPlayer
    extra = 0
    autocomplete_fields = ("player",)


@admin.register(MatchLineup)
class MatchLineupAdmin(ModelAdmin):

    list_display = (
        "match",
        "team",
        "side",
        "formation",
    )

    search_fields = ("match__home_team__name", "match__away_team__name", "team__name")
    autocomplete_fields = ("match", "team")
    inlines = [LineupPlayerInline]
    actions = [export_as_csv]