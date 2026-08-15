# aggregates/admin.py
from django.contrib import admin
from unfold.admin import ModelAdmin

from core.admin_actions import export_as_csv

from .models import PlayerMatchAggregate, CoachMatchAggregate, MatchAggregate


@admin.register(PlayerMatchAggregate)
class PlayerMatchAggregateAdmin(ModelAdmin):
    list_display = (
        'player', 'match', 'performance_score', 'maturity_score',
        'stability_index', 'clutch_index', 'total_votes'
    )
    list_filter = ('match', 'player__team')
    search_fields = ('player__first_name', 'player__last_name')
    autocomplete_fields = ('player', 'match')
    readonly_fields = (
        'performance_score', 'risk_index', 'maturity_score',
        'stability_index', 'clutch_index'
    )
    actions = [export_as_csv]


@admin.register(CoachMatchAggregate)
class CoachMatchAggregateAdmin(ModelAdmin):
    list_display = (
        'coach', 'match', 'average_score', 'total_votes'
    )
    list_filter = ('match', 'coach__team')
    search_fields = ('coach__first_name', 'coach__last_name')
    autocomplete_fields = ('coach', 'match')
    actions = [export_as_csv]


@admin.register(MatchAggregate)
class MatchAggregateAdmin(ModelAdmin):
    list_display = (
        'match', 'drama_index', 'avg_entertainment',
        'avg_tension', 'total_votes'
    )
    list_filter = ('match__league', 'match__season')
    search_fields = ('match__home_team__name', 'match__away_team__name')
    autocomplete_fields = ('match',)
    readonly_fields = ('drama_index',)
    actions = [export_as_csv]