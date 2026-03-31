# notifications/views.py
from django.shortcuts import redirect, get_object_or_404
from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import ListView, View
from django.contrib import messages
from django.utils import timezone
from django.http import HttpResponse
from django.template.loader import render_to_string
from .models import Notification

class NotificationListView(LoginRequiredMixin, ListView):
    model = Notification
    template_name = 'notifications/list.html'
    context_object_name = 'notifications'
    paginate_by = 20
    
    def get_queryset(self):
        return Notification.objects.filter(
            user=self.request.user
        ).select_related(
            'related_match__home_team',
            'related_match__away_team'
        ).order_by('-created_at')
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['unread_count'] = Notification.objects.filter(
            user=self.request.user,
            is_read=False
        ).count()
        context['page_title'] = 'Уведомления — DOPX'
        return context


class MarkAsReadView(LoginRequiredMixin, View):
    def post(self, request, pk):
        notification = get_object_or_404(Notification, pk=pk, user=request.user)
        if not notification.is_read:
            notification.is_read = True
            notification.save(update_fields=['is_read', 'updated_at'])
        if request.headers.get('HX-Request'):
            return HttpResponse('')
        return redirect('notifications:list')


class MarkAllAsReadView(LoginRequiredMixin, View):
    def post(self, request):
        count = Notification.objects.filter(
            user=request.user,
            is_read=False
        ).update(is_read=True, updated_at=timezone.now())
        messages.success(request, f'✅ Все {count} уведомлений отмечены как прочитанные')
        return redirect('notifications:list')


class NotificationBadgePartialView(LoginRequiredMixin, View):
    def get(self, request):
        unread_count = request.user.notifications.filter(is_read=False).count()
        html = render_to_string('components/_notification_badge.html', {
            'count': unread_count,
            'user': request.user,
        })
        return HttpResponse(html)