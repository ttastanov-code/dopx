# aggregates/admin.py
from django.contrib import admin
from unfold.admin import ModelAdmin

from core.admin_actions import export_as_csv

from .models import (
    CoachMatchAggregate,
    MatchAggregate,
    PlayerMatchAggregate,
    RefereeMatchAggregate,
    TeamMatchAggregate,
    TeamRatingCorrection,
)


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
        'stability_index', 'clutch_index', 'total_votes',
        'own_fans_avg', 'rival_fans_avg', 'neutral_avg',
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
    readonly_fields = ('total_votes', 'own_fans_avg', 'rival_fans_avg', 'neutral_avg')
    actions = [export_as_csv]


@admin.register(TeamMatchAggregate)
class TeamMatchAggregateAdmin(ModelAdmin):
    list_display = (
        'team', 'match', 'performance_score', 'total_votes',
        'own_fans_avg', 'rival_fans_avg', 'neutral_avg',
    )
    list_filter = ('match',)
    search_fields = ('team__name',)
    autocomplete_fields = ('team', 'match')
    readonly_fields = ('performance_score', 'total_votes', 'own_fans_avg', 'rival_fans_avg', 'neutral_avg')
    actions = [export_as_csv]


@admin.register(RefereeMatchAggregate)
class RefereeMatchAggregateAdmin(ModelAdmin):
    list_display = (
        'referee', 'match', 'performance_score', 'total_votes',
        'home_fans_avg', 'away_fans_avg', 'neutral_avg',
    )
    list_filter = ('match',)
    search_fields = ('referee__first_name', 'referee__last_name')
    autocomplete_fields = ('referee', 'match')
    readonly_fields = ('performance_score', 'total_votes', 'home_fans_avg', 'away_fans_avg', 'neutral_avg')
    actions = [export_as_csv]


@admin.register(TeamRatingCorrection)
class TeamRatingCorrectionAdmin(ModelAdmin):
    """Текущие автопоправки от независимого внешнего сигнала (см. её
    докстринг) — для наглядности и ручного override через действие ниже.
    list_editable на correction: можно обнулить/поправить руками сразу,
    не дожидаясь следующего ночного прогона detect_rating_stats_
    divergence_task."""

    list_display = ('team', 'correction', 'last_pattern', 'updated_at')
    search_fields = ('team__name',)
    list_editable = ('correction',)
    autocomplete_fields = ('team',)
    actions = ['reset_to_zero', export_as_csv]

    @admin.action(description="Обнулить поправку (не корректировать команду)")
    def reset_to_zero(self, request, queryset):
        updated = queryset.update(correction=0.0, last_pattern='')
        self.message_user(request, f"Поправка обнулена: {updated}")


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