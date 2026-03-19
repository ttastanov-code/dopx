# evaluations/urls.py
from django.urls import path
from .views import (
    EvaluateContextView, EvaluateTeamsView, EvaluatePlayersView,
    EvaluateCoachesView, EvaluateRefereeView, EvaluateMatchFinalView,
    EvaluationCompleteView,
)

app_name = 'evaluations'

urlpatterns = [
    path('match/<uuid:match_id>/context/', EvaluateContextView.as_view(), name='context'),
    path('match/<uuid:match_id>/teams/', EvaluateTeamsView.as_view(), name='teams'),
    path('match/<uuid:match_id>/players/', EvaluatePlayersView.as_view(), name='players'),
    path('match/<uuid:match_id>/coaches/', EvaluateCoachesView.as_view(), name='coaches'),
    path('match/<uuid:match_id>/referee/', EvaluateRefereeView.as_view(), name='referee'),
    path('match/<uuid:match_id>/match/', EvaluateMatchFinalView.as_view(), name='match_eval'),
    path('complete/<uuid:match_id>/', EvaluationCompleteView.as_view(), name='complete'),
]