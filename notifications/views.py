# notifications/views.py
from django.shortcuts import redirect, get_object_or_404
from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import ListView, View
from django.contrib import messages
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.contrib.admin.views.decorators import staff_member_required
from django.http import FileResponse, Http404, HttpResponse
from django.template.loader import render_to_string
from core.models import get_setting
from .models import Notification


MONTHS_NOM = ["", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль", "Август", "Сентябрь",
              "Октябрь", "Ноябрь", "Декабрь"]


def _day_group(created_at, today) -> str:
    """Подпись группы ленты: Сегодня, Вчера, На этой неделе, затем месяцы."""
    day = timezone.localtime(created_at).date()
    delta = (today - day).days
    if delta <= 0:
        return "Сегодня"
    if delta == 1:
        return "Вчера"
    if delta < 7:
        return "На этой неделе"
    label = MONTHS_NOM[day.month]
    return label if day.year == today.year else f"{label} {day.year}"


def _filtered(user, key: str):
    from .styles import SECTIONS, TYPE_STYLE, types_in

    qs = Notification.objects.filter(user=user)
    if key == "unread":
        return qs.filter(is_read=False)
    if key == "service":
        # «Сервис» — всё, что не попало в другие разделы, включая неизвестные типы.
        known_other = [t for t, (_i, _c, sec) in TYPE_STYLE.items() if sec != "service"]
        return qs.exclude(notification_type__in=known_other)
    if key in dict(SECTIONS):
        return qs.filter(notification_type__in=types_in(key))
    return qs


class NotificationListView(LoginRequiredMixin, ListView):
    """Лента уведомлений: вкладки по разделам, группы по дням, «Показать ещё» без перезагрузки."""
    model = Notification
    template_name = 'notifications/list.html'
    context_object_name = 'notifications'
    paginate_by = 20

    def get_paginate_by(self, queryset):
        return get_setting("user_notifications_page_size", self.paginate_by)

    def get_template_names(self):
        # «Показать ещё» — только следующая порция ленты.
        if self.request.headers.get('HX-Request') and self.request.GET.get('page'):
            return ['notifications/_feed_page.html']
        return [self.template_name]

    def current_filter(self) -> str:
        from .styles import SECTIONS

        key = self.request.GET.get('f', '')
        # Старые ссылки ?status=unread.
        if not key and self.request.GET.get('status') == 'unread':
            key = 'unread'
        return key if key in {'unread', *dict(SECTIONS)} else ''

    def get_queryset(self):
        return _filtered(self.request.user, self.current_filter()).select_related(
            'related_match__home_team', 'related_match__away_team'
        ).order_by('-created_at')

    def get_context_data(self, **kwargs):
        from .styles import SECTIONS

        context = super().get_context_data(**kwargs)
        user = self.request.user
        today = timezone.localdate()

        # Группы по дням; группа, продолжающая прошлую порцию, — без заголовка.
        groups = []
        for n in context['notifications']:
            label = _day_group(n.created_at, today)
            if groups and groups[-1]['label'] == label:
                groups[-1]['items'].append(n)
            else:
                groups.append({'label': label, 'items': [n]})
        if groups and groups[0]['label'] == self.request.GET.get('g', ''):
            groups[0]['continued'] = True
        context['groups'] = groups

        unread = Notification.objects.filter(user=user, is_read=False).count()
        tabs = [{'key': '', 'label': 'Все', 'count': None},
                {'key': 'unread', 'label': 'Непрочитанные', 'count': unread}]
        # Разделы — только те, где есть уведомления.
        for sec_key, sec_label in SECTIONS:
            if _filtered(user, sec_key).exists():
                tabs.append({'key': sec_key, 'label': sec_label, 'count': None})
        context.update({
            'tabs': tabs,
            'current_filter': self.current_filter(),
            'unread_count': unread,
            'has_any': Notification.objects.filter(user=user).exists(),
            'page_title': 'Уведомления — DOPX',
        })
        return context


def _oob_counters_html(user) -> str:
    """OOB-счётчики после отметки прочитанным: колокольчик, шапка панели, подзаголовок и вкладка страницы.
    Отсутствующий на странице id просто игнорируется.
    """
    unread_count = user.notifications.filter(is_read=False).count()
    badge_html = render_to_string('components/_notification_unread_badge.html', {
        'count': unread_count, 'oob': True,
    })
    return badge_html + render_to_string('notifications/_counters_oob.html', {'unread_count': unread_count})


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
                'notifications': request.user.notifications.all()[:8],
                'count': request.user.notifications.filter(is_read=False).count(),
            }, request=request)
            return HttpResponse(items_html + _oob_counters_html(request.user))

        messages.success(request, f'Все {count} уведомлений отмечены как прочитанные.')
        return redirect('notifications:list')


class UnreadCountBadgeView(View):
    """Партиал счётчика на колокольчике (поллинг каждые 30 с). Гость (сессия истекла) — пусто, без редиректа на вход."""
    def get(self, request):
        if not request.user.is_authenticated:
            return HttpResponse("")
        unread_count = request.user.notifications.filter(is_read=False).count()
        html = render_to_string('components/_notification_unread_badge.html', {
            'count': unread_count,
        })
        return HttpResponse(html)


class NotificationPanelView(View):
    """Панель колокольчика (число непрочитанных + последние 5) — при открытии и смене данных."""
    def get(self, request):
        if not request.user.is_authenticated:
            return HttpResponse("")
        html = render_to_string('components/_notification_panel.html', {
            'count': request.user.notifications.filter(is_read=False).count(),
            'notifications': request.user.notifications.all()[:8],
        }, request=request)
        return HttpResponse(html)


class NotificationBadgePartialView(View):
    def get(self, request):
        if not request.user.is_authenticated:
            return HttpResponse("")
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
