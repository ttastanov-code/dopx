from django.urls import path
from .views import (
    NotificationListView,
    MarkAsReadView,
    MarkAllAsReadView,
    NotificationBadgePartialView,
    UnreadCountBadgeView,
    contact_attachment_download,
)

app_name = 'notifications'

urlpatterns = [
    path('', NotificationListView.as_view(), name='list'),
    path('<uuid:pk>/read/', MarkAsReadView.as_view(), name='read'),
    path('read-all/', MarkAllAsReadView.as_view(), name='read-all'),
    path('badge-partial/', NotificationBadgePartialView.as_view(), name='badge_partial'),
    # Счётчик непрочитанных (UnreadCountBadgeView).
    path('unread-count/', UnreadCountBadgeView.as_view(), name='unread_count_partial'),
    path('contact-attachment/<uuid:pk>/', contact_attachment_download, name='contact_attachment'),
]