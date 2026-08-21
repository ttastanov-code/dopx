# notifications/views.py
from datetime import timedelta

from django.shortcuts import redirect, get_object_or_404
from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import ListView, View
from django.contrib import messages
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
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

        # notifications (context_object_name) — уже нарезанный пагинацией
        # QuerySet, у него нет .paginator. Общий счётчик — context['paginator'].
        total_count = context['paginator'].count if context.get('paginator') else context['notifications'].count()
        context['total_count'] = total_count
        context['read_count'] = max(total_count - context['unread_count'], 0)

        # Не {{ notifications|length }} — это длина текущей страницы пагинации.
        context['week_count'] = Notification.objects.filter(
            user=self.request.user,
            created_at__gte=timezone.now() - timedelta(days=7)
        ).count()

        context['page_title'] = 'Уведомления — DOPX'
        context['current_type'] = self.request.GET.get('type', '')
        context['current_status'] = self.request.GET.get('status', '')
        return context


def _oob_counters_html(user) -> str:
    """
    Общие "хвостовые" OOB-фрагменты (out-of-band swap), которые HTMX-ответы
    отметки прочитанным ВСЕГДА добавляют вслед за основным контентом —
    ПЕРЕСОБРАНО 2026-08-21 по прямому запросу пользователя ("кнопки кривые,
    непонятно, нельзя прочитать прямо в дропдауне"). Раньше отметка
    прочитанным нигде не обновляла счётчик на колокольчике мгновенно — сам
    механизм "живого" обновления был мёртвым кодом (JS слушал htmx:afterSwap
    на #notification-badge-container, элемента с таким id не существовало
    нигде в DOM, см. подробности в components/_notification_unread_badge.html).

    Три цели по id, которые обновляет htmx через hx-swap-oob="true"
    (безвредно, если конкретного id сейчас нет на странице — OOB-фрагмент
    просто игнорируется):
      1. #notif-unread-badge — красный счётчик на колокольчике.
      2. #notif-count-text — "N непрочитанных" в шапке дропдауна (если открыт).
      3. #stat-unread-count / #stat-read-count — карточки статистики на
         полной странице /notifications/ (если сейчас на ней).

    Вызывается из МЕСТ, где сработала любая отметка прочитанным — и из
    одиночной (MarkAsReadView), и из массовой (MarkAllAsReadView) — чтобы
    поведение было идентичным независимо от того, где кликнул пользователь:
    в дропдауне колокольчика или на полной странице.
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
    """
    ПЕРЕСОБРАНО 2026-08-21 — раньше единственный способ "перейти по
    уведомлению" был анкор с hx-post + hx-swap="none": хрупкий паттерн,
    у которого нет гарантии, что htmx не перехватит клик по <a href> и не
    отменит обычную навигацию браузера (htmx это делает для ссылок с
    hx-атрибутами в некоторых конфигурациях). Теперь переход — это ОБЫЧНЫЙ
    `<form method="post">` с `next` в скрытом поле: работает без единой
    строчки JS/htmx, 100% предсказуемо. `next` проверяется через
    `url_has_allowed_host_and_scheme` (open redirect защита), хотя сейчас
    `action_url` всегда генерируется на сервере — задел на будущее, если
    появится сценарий, где значение может прийти менее доверенным путём.

    Отдельно — HTMX-запрос от маленькой кнопки "отметить прочитанным без
    перехода" (components/_notification_item.html) — возвращает саму
    строку уведомления в новом (прочитанном) виде ПЛЮС OOB-хвост счётчиков
    (см. `_oob_counters_html`). `compact` в query string — тот же флаг, что
    и в шаблоне партиала: определяет, какой вариант вёрстки перерендерить
    (дропдаун колокольчика/полная страница), чтобы ответ визуально совпадал
    с тем, что уже было в DOM.
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

        # Возвращаемся с сохранением фильтров
        referer = request.META.get('HTTP_REFERER', '')
        if 'notifications' in referer:
            return redirect(referer)
        return redirect('notifications:list')


class MarkAllAsReadView(LoginRequiredMixin, View):
    """
    HTMX-ветка добавлена 2026-08-21 — раньше "Прочитать все" существовала
    ТОЛЬКО на полной странице (обычный POST + редирект), в дропдауне
    колокольчика такой кнопки не было вообще. Дропдаун теперь тоже её
    получил: HTMX-запрос перерисовывает превью последних 5 уведомлений
    (все уже прочитаны) на месте, без закрытия дропдауна и без перезагрузки
    страницы. Обычный (не-HTMX) POST с полной страницы — поведение не
    менялось: mark-all уведомлений (не только видимых на текущей
    странице/фильтре — это осознанно "все", не "все отфильтрованные"),
    полный редирект.
    """
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

        messages.success(request, f'✅ Все {count} уведомлений отмечены как прочитанные')
        return redirect('notifications:list')


class UnreadCountBadgeView(LoginRequiredMixin, View):
    """
    Лёгкий партиал ТОЛЬКО счётчика на колокольчике (components/
    _notification_unread_badge.html) — добавлено 2026-08-21. Раньше
    "живое" обновление количества было мёртвым кодом (см. докстринг
    _oob_counters_html выше) — счётчик обновлялся только на полной
    перезагрузке страницы. Теперь сам счётчик — самостоятельный маленький
    HTMX-виджет (`hx-trigger="every 30s"`), не требует перерисовки всего
    дропдауна (что закрывало бы его, если он открыт в момент поллинга —
    Alpine `open`-состояние живёт на родительском узле, вне этого поддерева).
    """
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