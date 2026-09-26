# matches/urls.py
from django.urls import path
from .views import (
    MatchListView, MatchDetailView, match_events_partial, match_header_partial, match_lineups_partial,
    match_card_partial, react_to_match,
)

app_name = 'matches'

urlpatterns = [
    path('', MatchListView.as_view(), name='list'),
    path('<uuid:pk>/', MatchDetailView.as_view(), name='detail'),
    path('<uuid:match_id>/events/', match_events_partial, name='events'),
    # Live-поллинг счёта/статуса — см. matches/_match_header.html
    path('<uuid:match_id>/header/', match_header_partial, name='header'),
    path('<uuid:match_id>/lineups/', match_lineups_partial, name='lineups'),
    # Live-поллинг карточки матча на главной/в списке — см. components/_match_card.html
    path('<uuid:match_id>/card/', match_card_partial, name='card'),
    # Реакция на завершённый матч (_reaction_widget_compact.html).
    path('<uuid:match_id>/react/', react_to_match, name='react'),
]