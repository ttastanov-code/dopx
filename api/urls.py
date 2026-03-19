# api/urls.py
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    ContextEvaluationViewSet,
    TeamEvaluationViewSet,
    PlayerEvaluationViewSet,
    CoachEvaluationViewSet,
    RefereeEvaluationViewSet,
    MatchEvaluationViewSet,
    MatchAggregateViewSet,
    PlayerAggregateViewSet,
    CoachAggregateViewSet,
)

app_name = 'api'
router = DefaultRouter()

# Оценки
router.register(r'context', ContextEvaluationViewSet, basename='context-eval')
router.register(r'team', TeamEvaluationViewSet, basename='team-eval')
router.register(r'player', PlayerEvaluationViewSet, basename='player-eval')
router.register(r'coach', CoachEvaluationViewSet, basename='coach-eval')
router.register(r'referee', RefereeEvaluationViewSet, basename='referee-eval')
router.register(r'match-eval', MatchEvaluationViewSet, basename='match-eval')

# Агрегаты (только чтение)
router.register(r'match-aggregate', MatchAggregateViewSet, basename='match-aggregate')
router.register(r'player-aggregate', PlayerAggregateViewSet, basename='player-aggregate')
router.register(r'coach-aggregate', CoachAggregateViewSet, basename='coach-aggregate')

urlpatterns = [
    path('', include(router.urls)),
]