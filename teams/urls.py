# teams/urls.py
from django.urls import path
from .views import TeamListView, TeamDetailView, team_rating_widget

app_name = 'teams'

urlpatterns = [
    path('', TeamListView.as_view(), name='list'),
    path('<uuid:pk>/', TeamDetailView.as_view(), name='detail'),
    path('<uuid:pk>/widget/', team_rating_widget, name='widget'),
]