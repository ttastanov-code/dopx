# teams/urls.py
from django.urls import path
from .views import TeamListView, TeamDetailView

app_name = 'teams'

urlpatterns = [
    path('', TeamListView.as_view(), name='list'),
    path('<uuid:pk>/', TeamDetailView.as_view(), name='detail'),
]