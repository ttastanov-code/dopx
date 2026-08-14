# notifications/views.py
from datetime import timedelta

from django.shortcuts import redirect, get_object_or_404
from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import ListView, View
from django.contrib import messages
from django.utils import timezone
from django.http import HttpResponse
from django.template.loader import render_to_string
from django.db.models import Q
from urllib.parse import urlencode
from .models import Notification


class NotificationListView(LoginRequiredMixin, ListView):
    model = Notification
    template_name = 'notifications/list.html'
    context_object_name = 'notifications'
    paginate_by = 20

    def get_queryset(self):
        queryset = Notification.objects.filter(
            user=self.request.user
        ).select_related(
            'related_match__home_team',
            'related_match__away_team'
        )
        
        # === ФИЛЬТР ПО ТИПУ УВЕДОМЛЕНИЯ ===
        notification_type = self.request.GET.get('type')
        if notification_type:
            type_map = {
                'match': ['match_finished', 'voting_open', 'voting_closing', 'aggregate_updated', 'top_performance'],
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
        
        # Сохраняем параметры фильтров для пагинации
        query_params = self.request.GET.copy()
        if 'page' in query_params:
            del query_params['page']
        context['query_params'] = f"&{query_params.urlencode()}" if query_params else ''
        
        context['unread_count'] = Notification.objects.filter(
            user=self.request.user,
            is_read=False
        ).count()

        # ИСПРАВЛЕНО: карточка "Всего" в шаблоне раньше читала
        # `notifications.paginator.count` — но `notifications` (context_object_name)
        # это уже НАРЕЗАННЫЙ пагинацией QuerySet (список объектов текущей
        # страницы), у него нет атрибута `.paginator` (он есть только у
        # объекта `Paginator`/`Page`, отдельно кладущегося ListView'ом в
        # контекст как ключ `paginator`). Обращение к несуществующему
        # атрибуту в шаблоне Django молча резолвится в пустую строку —
        # `|default:0` подхватывал именно эту пустую строку и рисовал "0",
        # даже когда уведомления реально были. Настоящий общий счётчик по
        # текущим фильтрам — `context['paginator'].count` (paginator уже
        # положен в context базовым ListView.get_context_data() выше).
        total_count = context['paginator'].count if context.get('paginator') else context['notifications'].count()
        context['total_count'] = total_count
        context['read_count'] = max(total_count - context['unread_count'], 0)

        # ИСПРАВЛЕНО: карточка "За неделю" раньше показывала
        # `{{ notifications|length }}` — это длина ТЕКУЩЕЙ СТРАНИЦЫ
        # пагинации (максимум paginate_by=20), а не реальное количество
        # уведомлений за последние 7 дней. Считаем честно от текущей даты.
        context['week_count'] = Notification.objects.filter(
            user=self.request.user,
            created_at__gte=timezone.now() - timedelta(days=7)
        ).count()

        context['page_title'] = 'Уведомления — DOPX'
        context['current_type'] = self.request.GET.get('type', '')
        context['current_status'] = self.request.GET.get('status', '')
        return context


class MarkAsReadView(LoginRequiredMixin, View):
    def post(self, request, pk):
        notification = get_object_or_404(Notification, pk=pk, user=request.user)
        if not notification.is_read:
            notification.is_read = True
            notification.save(update_fields=['is_read', 'updated_at'])
        
        if request.headers.get('HX-Request'):
            return HttpResponse('')
        
        # Возвращаемся с сохранением фильтров
        referer = request.META.get('HTTP_REFERER', '')
        if 'notifications' in referer:
            return redirect(referer)
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