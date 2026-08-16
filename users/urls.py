# users/urls.py
from django.urls import path
from .views import (
    RegisterView, LoginView, LogoutView, ProfileView, PublicProfileView, BadgeCatalogView,
    UserLeaderboardView, PlayerLeaderboardView,
    ProfileEditView, PasswordChangeViewCustom,
    PasswordResetViewCustom, PasswordResetDoneViewCustom,
    PasswordResetConfirmViewCustom, PasswordResetCompleteViewCustom,
    NotificationSettingsView, VerifyEmailView, VerifyEmailSentView, VerifyEmailInvalidView,
    toggle_follow, push_subscribe, push_unsubscribe,
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
    path('achievements/', BadgeCatalogView.as_view(), name='badge_catalog'),
    
    # Верификация
    path('verify-email-sent/', VerifyEmailSentView.as_view(), name='verify_email_sent'),
    path('verify-email/invalid/', VerifyEmailInvalidView.as_view(), name='verify_email_invalid'),
    path('verify-email/<uuid:token>/', VerifyEmailView.as_view(), name='verify_email'),
    
    # Сброс пароля
    path('password-reset/', PasswordResetViewCustom.as_view(), name='password_reset'),
    path('password-reset/done/', PasswordResetDoneViewCustom.as_view(), name='password_reset_done'),
    path('password-reset-confirm/<uidb64>/<token>/', PasswordResetConfirmViewCustom.as_view(), name='password_reset_confirm'),
    path('password-reset-complete/', PasswordResetCompleteViewCustom.as_view(), name='password_reset_complete'),
    
    path('leaderboard/', UserLeaderboardView.as_view(), name='leaderboard'),
    path('players/leaderboard/', PlayerLeaderboardView.as_view(), name='player_leaderboard'),

    # Follow-граф — один эндпоинт на оба типа цели, см. docstring toggle_follow.
    path('follow/<str:target_type>/<uuid:target_id>/', toggle_follow, name='toggle_follow'),

    # Web Push — см. static/js/push.js.
    path('push/subscribe/', push_subscribe, name='push_subscribe'),
    path('push/unsubscribe/', push_unsubscribe, name='push_unsubscribe'),

    # Публичный профиль по username (только чтение), см. докстринг
    # PublicProfileView. Префикс 'u/', а не 'profile/<username>/' — иначе
    # пересечётся с 'profile/edit/', 'profile/password/' и т.д.
    path('u/<str:username>/', PublicProfileView.as_view(), name='public_profile'),
]