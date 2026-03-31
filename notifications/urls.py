from django.urls import path
from .views import (
    NotificationListView,
    MarkAsReadView,
    MarkAllAsReadView,
    NotificationBadgePartialView,
)

app_name = 'notifications'

urlpatterns = [
    path('', NotificationListView.as_view(), name='list'),
    path('<uuid:pk>/read/', MarkAsReadView.as_view(), name='read'),
    path('read-all/', MarkAllAsReadView.as_view(), name='read-all'),
    path('badge-partial/', NotificationBadgePartialView.as_view(), name='badge_partial'),
]