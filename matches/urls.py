# matches/urls.py
from django.urls import path
from .views import MatchListView, MatchDetailView

app_name = 'matches'

urlpatterns = [
    path('', MatchListView.as_view(), name='list'),
    path('<uuid:pk>/', MatchDetailView.as_view(), name='detail'),
]