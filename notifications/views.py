# notifications/views.py
from datetime import timedelta

from django.shortcuts import redirect, get_object_or_404
from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import ListView, View
from django.contrib import messages
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.contrib.admin.views.decorators import staff_member_required
from django.http import FileResponse, Http404, HttpResponse
from django.template.loader import render_to_string
from django.db.models import Q
from urllib.parse import urlencode
from core.models import get_setting
from .models import Notification


class NotificationListView(LoginRequiredMixin, ListView):
    model = Notification
    template_name = 'notifications/list.html'
    context_object_name = 'notifications'
    paginate_by = 20

    def get_paginate_by(self, queryset):
        # Размер страницы — из настроек платформы.
        return get_setting("user_notifications_page_size", self.paginate_by)

    def get_queryset(self):
        queryset = Notification.objects.filter(
            user=self.request.user
        ).select_related(
            'related_match__home_team',
            'related_match__away_team'
        )
        
        # === ФИЛЬТР ПО ТИПУ ===
        notification_type = self.request.GET.get('type')
        if notification_type:
            type_map = {
                'match': ['match_finished', 'voting_open', 'voting_closing', 'aggregate_updated', 'top_performance', 'match_event'],
                'voting': ['voting_open', 'voting_closing'],
                'badge': ['new_badge', 'level_up'],
                'system': ['system', 'verification_required'],
            }
            if notification_type in type_map:
                queryset = queryset.filter(notification_type__in=type_map[notification_type])
        
        # === ФИЛЬТР ПО СТАТУСУ ПРОЧТЕНИЯ ===
        status = self.request.GET.get('status')
        if status == 'unread':
            queryset = queryset.filter(is_read=False)
        elif status == 'read':
            queryset = queryset.filter(is_read=True)
        
        return queryset.order_by('-created_at')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        
        # Параметры фильтров для пагинации
        query_params = self.request.GET.copy()
        if 'page' in query_params:
            del query_params['page']
        context['query_params'] = f"&{query_params.urlencode()}" if query_params else ''
        
        context['unread_count'] = Notification.objects.filter(
            user=self.request.user,
            is_read=False
        ).count()

        # Общее число — из context['paginator'].
        total_count = context['paginator'].count if context.get('paginator') else context['notifications'].count()
        context['total_count'] = total_count
        context['read_count'] = max(total_count - context['unread_count'], 0)

        # Не |length — это только текущая страница.
        context['week_count'] = Notification.objects.filter(
            user=self.request.user,
            created_at__gte=timezone.now() - timedelta(days=7)
        ).count()

        context['page_title'] = 'Уведомления — DOPX'
        context['current_type'] = self.request.GET.get('type', '')
        context['current_status'] = self.request.GET.get('status', '')
        return context


def _oob_counters_html(user) -> str:
    """OOB-фрагменты счётчиков после отметки прочитанным:
    #notif-unread-badge, #notif-count-text, #stat-unread-count / #stat-read-count.
    Отсутствующий на странице id просто игнорируется.
    """
    unread_count = user.notifications.filter(is_read=False).count()
    total_count = user.notifications.count()
    read_count = max(total_count - unread_count, 0)

    badge_html = render_to_string('components/_notification_unread_badge.html', {
        'count': unread_count, 'oob': True,
    })
    count_text_html = (
        f'<div class="text-xs opacity-60" id="notif-count-text" hx-swap-oob="true">'
        f'{unread_count} непрочитанных</div>'
    )
    stat_cards_html = (
        f'<div id="stat-unread-count" hx-swap-oob="true" class="text-base md:text-xl font-bold text-warning">{unread_count}</div>'
        f'<div id="stat-read-count" hx-swap-oob="true" class="text-base md:text-xl font-bold text-success">{read_count}</div>'
    )
    return badge_html + count_text_html + stat_cards_html


class MarkAsReadView(LoginRequiredMixin, View):
    """Отметить прочитанным.
    Обычный POST — переход на next (проверяется от open redirect).
    HTMX — строка уведомления в новом виде + OOB-счётчики; compact — вариант вёрстки.
    """
    def post(self, request, pk):
        notification = get_object_or_404(Notification, pk=pk, user=request.user)
        if not notification.is_read:
            notification.is_read = True
            notification.save(update_fields=['is_read', 'updated_at'])

        if request.headers.get('HX-Request'):
            compact = request.GET.get('compact') == '1'
            item_html = render_to_string('components/_notification_item.html', {
                'notification': notification, 'compact': compact,
            }, request=request)
            return HttpResponse(item_html + _oob_counters_html(request.user))

        next_url = request.POST.get('next') or request.GET.get('next')
        if next_url and url_has_allowed_host_and_scheme(
            next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()
        ):
            return redirect(next_url)

        # Назад с сохранением фильтров
        referer = request.META.get('HTTP_REFERER', '')
        if 'notifications' in referer:
            return redirect(referer)
        return redirect('notifications:list')


class MarkAllAsReadView(LoginRequiredMixin, View):
    """Прочитать все. HTMX — перерисовка превью в дропдауне, обычный POST — редирект."""
    def post(self, request):
        count = Notification.objects.filter(
            user=request.user,
            is_read=False
        ).update(is_read=True, updated_at=timezone.now())

        if request.headers.get('HX-Request'):
            items_html = render_to_string('components/_notification_dropdown_items.html', {
                'notifications': request.user.notifications.all()[:5],
                'count': request.user.notifications.filter(is_read=False).count(),
            }, request=request)
            return HttpResponse(items_html + _oob_counters_html(request.user))

        messages.success(request, f'Все {count} уведомлений отмечены как прочитанные.')
        return redirect('notifications:list')


class UnreadCountBadgeView(LoginRequiredMixin, View):
    """Партиал счётчика на колокольчике (поллинг каждые 30 с)."""
    def get(self, request):
        unread_count = request.user.notifications.filter(is_read=False).count()
        html = render_to_string('components/_notification_unread_badge.html', {
            'count': unread_count,
        })
        return HttpResponse(html)


class NotificationBadgePartialView(LoginRequiredMixin, View):
    def get(self, request):
        unread_count = request.user.notifications.filter(is_read=False).count()
        html = render_to_string('components/_notification_badge.html', {
            'count': unread_count,
            'user': request.user,
        }, request=request)
        return HttpResponse(html)

@staff_member_required
def contact_attachment_download(request, pk):
    """Вложение обращения — только staff и только скачиванием (не открывается как страница)."""
    import os

    from .models import ContactSubmission

    submission = get_object_or_404(ContactSubmission, pk=pk)
    if not submission.attachment or not submission.attachment.storage.exists(submission.attachment.name):
        raise Http404("Вложение не найдено")
    response = FileResponse(
        submission.attachment.open('rb'), as_attachment=True,
        filename=os.path.basename(submission.attachment.name),
    )
    response['X-Content-Type-Options'] = 'nosniff'
    return response
