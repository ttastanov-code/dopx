# aggregates/admin.py
from django.contrib import admin
from .models import PlayerMatchAggregate, CoachMatchAggregate, MatchAggregate


@admin.register(PlayerMatchAggregate)
class PlayerMatchAggregateAdmin(admin.ModelAdmin):
    list_display = (
        'player', 'match', 'performance_score', 'maturity_score',
        'stability_index', 'clutch_index', 'total_votes'
    )
    list_filter = ('match', 'player__team')
    search_fields = ('player__first_name', 'player__last_name')
    readonly_fields = (
        'performance_score', 'risk_index', 'maturity_score',
        'stability_index', 'clutch_index'
    )


@admin.register(CoachMatchAggregate)
class CoachMatchAggregateAdmin(admin.ModelAdmin):
    list_display = (
        'coach', 'match', 'average_score', 'total_votes'
    )
    list_filter = ('match', 'coach__team')
    search_fields = ('coach__first_name', 'coach__last_name')


@admin.register(MatchAggregate)
class MatchAggregateAdmin(admin.ModelAdmin):
    list_display = (
        'match', 'drama_index', 'avg_entertainment',
        'avg_tension', 'total_votes'
    )
    list_filter = ('match__league', 'match__season')
    readonly_fields = ('drama_index',)