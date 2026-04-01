# users/urls.py
from django.urls import path
from .views import (
    RegisterView, LoginView, LogoutView, ProfileView,
    UserLeaderboardView, PlayerLeaderboardView,
    ProfileEditView, PasswordChangeViewCustom,
    PasswordResetViewCustom, PasswordResetDoneViewCustom,
    PasswordResetConfirmViewCustom, PasswordResetCompleteViewCustom,
    NotificationSettingsView,
)

app_name = 'users'

urlpatterns = [
    path('register/', RegisterView.as_view(), name='register'),
    path('login/', LoginView.as_view(), name='login'),
    path('logout/', LogoutView.as_view(), name='logout'),
    path('profile/', ProfileView.as_view(), name='profile'),
    path('profile/edit/', ProfileEditView.as_view(), name='profile_edit'),
    path('profile/password/', PasswordChangeViewCustom.as_view(), name='password_change'),
    path('profile/notifications/', NotificationSettingsView.as_view(), name='notification_settings'),
    
    # Сброс пароля
    path('password-reset/', PasswordResetViewCustom.as_view(), name='password_reset'),
    path('password-reset/done/', PasswordResetDoneViewCustom.as_view(), name='password_reset_done'),
    path('password-reset-confirm/<uidb64>/<token>/', PasswordResetConfirmViewCustom.as_view(), name='password_reset_confirm'),
    path('password-reset-complete/', PasswordResetCompleteViewCustom.as_view(), name='password_reset_complete'),
    
    path('leaderboard/', UserLeaderboardView.as_view(), name='leaderboard'),
    path('players/leaderboard/', PlayerLeaderboardView.as_view(), name='player_leaderboard'),
]