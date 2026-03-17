from django.contrib import admin
from .models import MatchEvent


@admin.register(MatchEvent)
class MatchEventAdmin(admin.ModelAdmin):

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
    )