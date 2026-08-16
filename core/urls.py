# core/urls.py
from django.urls import path
from .views import (
    HomeView, RulesView, ContactsView, PrivacyPolicyView, AntiFraudView,
    MatchShareCardView, standings_preview, handler_404, handler_500, service_worker,
)

app_name = 'core'

urlpatterns = [
    path('', HomeView.as_view(), name='home'),
    path('sw.js', service_worker, name='service_worker'),  # не /static/sw.js — см. docstring view
    path('rules/', RulesView.as_view(), name='rules'),
    path('privacy/', PrivacyPolicyView.as_view(), name='privacy'),
    path('contacts/', ContactsView.as_view(), name='contacts'),
    path('anti-fraud/', AntiFraudView.as_view(), name='anti_fraud'),
    path('share/match/<uuid:match_id>/card.png', MatchShareCardView.as_view(), name='match_share_card'),
    path('api/standings-preview/', standings_preview, name='standings_preview'),
]