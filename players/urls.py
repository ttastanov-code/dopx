# players/urls.py
from django.urls import path
from .views import PlayerListView, PlayerDetailView
from django.views.generic.base import RedirectView

app_name = 'players'

urlpatterns = [
    path('', PlayerListView.as_view(), name='list'),
    path('<uuid:pk>/', PlayerDetailView.as_view(), name='detail'),
    path('leaderboard/', RedirectView.as_view(pattern_name='users:player_leaderboard'), name='leaderboard'),
]