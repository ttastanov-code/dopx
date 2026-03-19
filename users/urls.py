# users/urls.py
from django.urls import path
from .views import (
    RegisterView, LoginView, LogoutView, ProfileView,
    UserLeaderboardView, PlayerLeaderboardView
)

app_name = 'users'

urlpatterns = [
    path('register/', RegisterView.as_view(), name='register'),
    path('login/', LoginView.as_view(), name='login'),
    path('logout/', LogoutView.as_view(), name='logout'),
    path('profile/', ProfileView.as_view(), name='profile'),
    path('leaderboard/', UserLeaderboardView.as_view(), name='leaderboard'),
    path('players/leaderboard/', PlayerLeaderboardView.as_view(), name='player_leaderboard'),
]