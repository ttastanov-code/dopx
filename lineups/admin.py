from django.contrib import admin
from .models import MatchLineup, MatchLineupPlayer


class LineupPlayerInline(admin.TabularInline):
    model = MatchLineupPlayer
    extra = 0


@admin.register(MatchLineup)
class MatchLineupAdmin(admin.ModelAdmin):

    list_display = (
        "match",
        "team",
        "side",
        "formation",
    )

    inlines = [LineupPlayerInline]