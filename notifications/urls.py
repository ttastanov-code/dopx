from django.urls import path
from .views import (
    NotificationListView,
    MarkAsReadView,
    MarkAllAsReadView,
    NotificationBadgePartialView,
    UnreadCountBadgeView,
)

app_name = 'notifications'

urlpatterns = [
    path('', NotificationListView.as_view(), name='list'),
    path('<uuid:pk>/read/', MarkAsReadView.as_view(), name='read'),
    path('read-all/', MarkAllAsReadView.as_view(), name='read-all'),
    path('badge-partial/', NotificationBadgePartialView.as_view(), name='badge_partial'),
    # Счётчик непрочитанных (UnreadCountBadgeView).
    path('unread-count/', UnreadCountBadgeView.as_view(), name='unread_count_partial'),
]