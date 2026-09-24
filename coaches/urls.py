# coaches/urls.py
# Список и страница тренера.
from django.urls import path
from coaches.views import CoachListView, CoachDetailView

app_name = 'coaches'

urlpatterns = [
    path('', CoachListView.as_view(), name='list'),
    path('<uuid:pk>/', CoachDetailView.as_view(), name='detail'),
]