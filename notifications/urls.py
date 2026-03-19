# notifications/urls.py
from django.urls import path
from .views import NotificationListView, MarkAsReadView, MarkAllAsReadView

app_name = 'notifications'

urlpatterns = [
    path('', NotificationListView.as_view(), name='list'),
    path('<int:pk>/read/', MarkAsReadView.as_view(), name='read'),
    path('read-all/', MarkAllAsReadView.as_view(), name='read-all'),
]