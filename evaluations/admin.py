from django.contrib import admin
from .models import (
    ContextEvaluation,
    TeamEvaluation,
    PlayerEvaluation,
    CoachEvaluation,
    RefereeEvaluation,
    MatchEvaluation
)


@admin.register(ContextEvaluation)
class ContextEvaluationAdmin(admin.ModelAdmin):
    list_display = ('user', 'match', 'watched_type', 'attended_stadium', 'created_at')
    list_filter = ('watched_type', 'attended_stadium')
    search_fields = ('user__username', 'match__home_team__name', 'match__away_team__name')


@admin.register(TeamEvaluation)
class TeamEvaluationAdmin(admin.ModelAdmin):
    list_display = ('user', 'match', 'team', 'average_score', 'created_at')
    list_filter = ('team',)
    search_fields = ('user__username', 'team__name')


@admin.register(PlayerEvaluation)
class PlayerEvaluationAdmin(admin.ModelAdmin):
    list_display = ('user', 'match', 'player', 'contribution', 'risk', 'potential', 'maturity_score')
    list_filter = ('match', 'player')
    search_fields = ('user__username', 'player__first_name', 'player__last_name')


@admin.register(CoachEvaluation)
class CoachEvaluationAdmin(admin.ModelAdmin):
    list_display = ('user', 'match', 'coach', 'average_score', 'created_at')
    list_filter = ('coach',)
    search_fields = ('user__username', 'coach__first_name', 'coach__last_name')


@admin.register(RefereeEvaluation)
class RefereeEvaluationAdmin(admin.ModelAdmin):
    list_display = ('user', 'match', 'influence_score', 'decision_quality', 'created_at')
    list_filter = ('match',)
    search_fields = ('user__username',)


@admin.register(MatchEvaluation)
class MatchEvaluationAdmin(admin.ModelAdmin):
    list_display = ('user', 'match', 'entertainment', 'tension', 'drama_index', 'created_at')
    list_filter = ('match', 'turning_point')
    search_fields = ('user__username',)