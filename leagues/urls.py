# leagues/urls.py
from django.urls import path
from leagues.views import LeagueListView, LeagueDetailView

app_name = 'leagues'

urlpatterns = [
    path('', LeagueListView.as_view(), name='list'),
    path('<uuid:pk>/', LeagueDetailView.as_view(), name='detail'),
]