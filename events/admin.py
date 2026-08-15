from django.contrib import admin
from unfold.admin import ModelAdmin

from core.admin_actions import export_as_csv

from .models import EventReaction, MatchEvent


@admin.register(MatchEvent)
class MatchEventAdmin(ModelAdmin):

    list_display = (
        "match",
        "event_type",
        "minute",
        "player",
        "team_side",
    )

    list_filter = (
        "event_type",
        "team_side",
    )

    search_fields = (
        "player__first_name",
        "player__last_name",
        "match__home_team__name",
        "match__away_team__name",
    )

    autocomplete_fields = ("match", "player", "assist_player", "player_out")
    actions = [export_as_csv]


@admin.register(EventReaction)
class EventReactionAdmin(ModelAdmin):
    list_display = ("match_event", "user", "reaction", "created_at")
    list_filter = ("reaction",)
    autocomplete_fields = ("user",)
    raw_id_fields = ("match_event",)
    actions = [export_as_csv]