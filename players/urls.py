# players/urls.py
from django.urls import path
from .views import (
    PlayerListView, PlayerDetailView, player_rating_widget,
    PlayerSeasonRecapView, player_season_recap_card,
)
from django.views.generic.base import RedirectView

app_name = 'players'

urlpatterns = [
    path('', PlayerListView.as_view(), name='list'),
    path('<uuid:pk>/', PlayerDetailView.as_view(), name='detail'),
    path('<uuid:pk>/widget/', player_rating_widget, name='widget'),
    # Season recap (продуктовый аудит, раздел 5d) — без season_id: текущий
    # активный сезон по умолчанию.
    path('<uuid:pk>/recap/', PlayerSeasonRecapView.as_view(), name='season_recap'),
    path('<uuid:pk>/recap/<uuid:season_id>/', PlayerSeasonRecapView.as_view(), name='season_recap'),
    path('<uuid:pk>/recap/<uuid:season_id>/card.png', player_season_recap_card, name='season_recap_card'),
    path('leaderboard/', RedirectView.as_view(pattern_name='users:player_leaderboard'), name='leaderboard'),
]