from django.contrib import admin
from django.utils.translation import gettext_lazy as _
from unfold.admin import ModelAdmin

from core.admin_actions import export_as_csv

from .models import (
    ContextEvaluation,
    TeamEvaluation,
    PlayerEvaluation,
    CoachEvaluation,
    RefereeEvaluation,
    MatchEvaluation,
    EvaluationSession,
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


@admin.register(EvaluationSession)
class EvaluationSessionAdmin(ModelAdmin):
    """
    БАГ, КОТОРЫЙ ТУТ БЫЛ: модель отслеживания прогресса вайзарда нигде не
    была зарегистрирована в админке — были видны только шесть моделей с
    самими оценками (ContextEvaluation/TeamEvaluation/...), а запись,
    которая реально решает "уже оценил / ещё нет"
    (status='completed' в EvaluationSession — см. gate в
    evaluations/views.py::EvaluateContextView.dispatch()), нигде не
    отображалась и не редактировалась. Из-за этого не было простого способа
    сбросить свою тестовую оценку и пройти вайзард заново — приходилось
    лезть напрямую в БД.
    """
    list_display = ('user', 'match', 'status', 'progress_percentage', 'started_at', 'completed_at', 'fill_duration_seconds')
    list_filter = ('status',)
    search_fields = (
        'user__username', 'user__email',
        'match__home_team__name', 'match__away_team__name',
    )
    autocomplete_fields = ('user', 'match')
    readonly_fields = ('started_at', 'completed_at', 'ip_address')
    actions = [export_as_csv]

    @admin.display(description=_('Прогресс'))
    def progress_percentage(self, obj):
        return f"{obj.progress_percentage()}%"

    @admin.display(description=_('Время заполнения (сек)'))
    def fill_duration_seconds(self, obj):
        return obj.fill_duration_seconds
