from django.contrib import admin
from unfold.admin import ModelAdmin

from core.admin_actions import export_as_csv

from .models import (
    ContextEvaluation,
    TeamEvaluation,
    PlayerEvaluation,
    CoachEvaluation,
    RefereeEvaluation,
    MatchEvaluation
)


@admin.register(ContextEvaluation)
class ContextEvaluationAdmin(ModelAdmin):
    list_display = ('user', 'match', 'watched_type', 'attended_stadium', 'created_at')
    list_filter = ('watched_type', 'attended_stadium')
    search_fields = ('user__username', 'match__home_team__name', 'match__away_team__name')
    autocomplete_fields = ('user', 'match', 'supported_team')
    actions = [export_as_csv]


@admin.register(TeamEvaluation)
class TeamEvaluationAdmin(ModelAdmin):
    list_display = ('user', 'match', 'team', 'average_score', 'created_at')
    list_filter = ('team',)
    search_fields = ('user__username', 'team__name')
    autocomplete_fields = ('user', 'match', 'team')
    actions = [export_as_csv]


@admin.register(PlayerEvaluation)
class PlayerEvaluationAdmin(ModelAdmin):
    list_display = ('user', 'match', 'player', 'contribution', 'risk', 'potential', 'maturity_score')
    list_filter = ('match', 'player')
    search_fields = ('user__username', 'player__first_name', 'player__last_name')
    autocomplete_fields = ('user', 'match', 'player')
    actions = [export_as_csv]


@admin.register(CoachEvaluation)
class CoachEvaluationAdmin(ModelAdmin):
    list_display = ('user', 'match', 'coach', 'average_score', 'created_at')
    list_filter = ('coach',)
    search_fields = ('user__username', 'coach__first_name', 'coach__last_name')
    autocomplete_fields = ('user', 'match', 'coach')
    actions = [export_as_csv]


@admin.register(RefereeEvaluation)
class RefereeEvaluationAdmin(ModelAdmin):
    list_display = ('user', 'match', 'influence_score', 'decision_quality', 'created_at')
    list_filter = ('match',)
    search_fields = ('user__username',)
    autocomplete_fields = ('user', 'match')
    actions = [export_as_csv]


@admin.register(MatchEvaluation)
class MatchEvaluationAdmin(ModelAdmin):
    list_display = ('user', 'match', 'entertainment', 'tension', 'drama_index', 'created_at')
    list_filter = ('match', 'turning_point')
    search_fields = ('user__username',)
    autocomplete_fields = ('user', 'match')
    actions = [export_as_csv]
