# notifications/admin.py
from django.contrib import admin
from unfold.admin import ModelAdmin
from django.utils.html import format_html, strip_tags
from django.utils.safestring import mark_safe
from django.urls import reverse
from django.utils import timezone
from django import forms
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.conf import settings
import logging
import os

from core.admin_actions import export_as_csv

from .models import Notification, ContactSubmission

logger = logging.getLogger(__name__)


@admin.register(Notification)
class NotificationAdmin(ModelAdmin):
    list_display = ('id_short', 'user_link', 'title', 'notification_type', 'is_read_badge', 'created_at')
    list_filter = ('notification_type', 'is_read', 'created_at')
    search_fields = ('title', 'message', 'user__username', 'user__email')
    readonly_fields = ('created_at', 'updated_at')
    autocomplete_fields = ('user', 'related_match')
    actions = [export_as_csv]
    
    def id_short(self, obj):
        return format_html('<code>#{}</code>', str(obj.id)[:8])
    
    def user_link(self, obj):
        if obj.user:
            url = reverse('admin:users_user_change', args=[obj.user.pk])
            return format_html('<a href="{}">{}</a>', url, obj.user.username)
        return '—'
    
    def is_read_badge(self, obj):
        # БАГ, КОТОРЫЙ ТУТ БЫЛ: format_html() без единого аргумента/kwarg
        # (строка — просто статичная разметка, подставлять нечего) — в
        # Django 6 это TypeError "args or kwargs must be provided"
        # (django/utils/html.py::format_html), а не молчаливый no-op, как
        # было раньше. format_html специально требует хотя бы один
        # экранируемый аргумент — иначе это неотличимо от случайного
        # format_html(user_input), который выглядел бы "безопасным", но
        # не экранировал бы ничего. Раз подставлять действительно нечего —
        # используем mark_safe напрямую на статичной, целиком нашей же
        # HTML-строке (без пользовательского ввода внутри).
        if obj.is_read:
            return mark_safe('<span style="color:#10b981;">✓ Прочитано</span>')
        return mark_safe('<span style="color:#f59e0b;">● Непрочитано</span>')


class ContactSubmissionForm(forms.ModelForm):
    """Форма обращения с опцией отправки email при изменении статуса"""
    send_status_email = forms.BooleanField(
        required=False,
        initial=True,
        label='📧 Уведомить пользователя об изменении статуса',
        help_text='Отправить email при изменении статуса обращения'
    )
    
    class Meta:
        model = ContactSubmission
        fields = '__all__'
        widgets = {
            'message': forms.Textarea(attrs={'rows': 4, 'readonly': 'readonly'}),
            'admin_response': forms.Textarea(attrs={
                'rows': 3,
                'class': 'vLargeTextField',
                'placeholder': 'Внутренний ответ для истории...'
            }),
        }


@admin.register(ContactSubmission)
class ContactSubmissionAdmin(ModelAdmin):
    form = ContactSubmissionForm
    
    list_display = (
        'id_short',
        'contact_email',
        'subject',
        'category',
        'status_badge',
        'attachment_link',
        'created_at',
    )
    list_filter = ('status', 'category', 'created_at', 'user__is_verified')
    search_fields = ('subject', 'message', 'user__username', 'guest_email')
    readonly_fields = (
        'created_at', 
        'updated_at', 
        'ip_address', 
        'user_agent',
        'attachment_link',
        'contact_email',
    )
    date_hierarchy = 'created_at'
    list_per_page = 30
    
    fieldsets = (
        ('Информация о пользователе', {
            'fields': ('user', 'contact_email', 'ip_address', 'user_agent')
        }),
        ('Данные обращения', {
            'fields': ('category', 'subject', 'message', 'attachment', 'attachment_link')
        }),
        ('Статус', {
            'fields': ('status', 'send_status_email')
        }),
        ('Мета', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
    
    def id_short(self, obj):
        return format_html('<code>#{}</code>', str(obj.id)[:8])
    id_short.short_description = 'ID'
    
    def contact_email(self, obj):
        return obj.contact_email
    contact_email.short_description = 'Email'
    
    def status_badge(self, obj):
        colors = {
            'new': '#3b82f6',
            'in_progress': '#f59e0b',
            'resolved': '#10b981',
            'closed': '#6b7280',
        }
        color = colors.get(obj.status, '#6b7280')
        return format_html(
            '<span style="color:{};font-weight:bold;">{}</span>',
            color,
            obj.get_status_display()
        )
    status_badge.short_description = 'Статус'
    
    def attachment_link(self, obj):
        """Показывает ссылку на файл"""
        if obj.attachment and obj.attachment.storage.exists(obj.attachment.name):
            filename = os.path.basename(obj.attachment.name)
            return format_html(
                '<a href="{}" target="_blank" style="color:#3b82f6;text-decoration:underline;">📎 Скачать {}</a>',
                obj.attachment.url,
                filename
            )
        return '—'
    attachment_link.short_description = 'Файл'
    
    def save_model(self, request, obj, form, change):
        """Сохранение модели + отправка email при изменении статуса"""
        old_status = None
        if change:
            try:
                old_obj = ContactSubmission.objects.get(pk=obj.pk)
                old_status = old_obj.status
            except ContactSubmission.DoesNotExist:
                pass
        
        # Сохраняем объект
        super().save_model(request, obj, form, change)
        
        # Отправляем email при изменении статуса (если отмечено)
        if change and old_status != obj.status:
            send_status_email = form.cleaned_data.get('send_status_email', False)
            if send_status_email and obj.contact_email:
                self._send_status_change_email(obj, old_status, request)
    
    def _send_status_change_email(self, ticket: ContactSubmission, old_status: str, request=None):
        """Отправка email при изменении статуса обращения"""
        try:
            recipient = ticket.contact_email
            if not recipient:
                logger.warning(f"No email for ticket {ticket.id}")
                return
            
            from_email = getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@dopx.kz')
            site_url = getattr(settings, 'SITE_URL', 'https://dopx.kz')
            
            status_display = dict(ContactSubmission.STATUS_CHOICES).get(ticket.status, ticket.status)
            old_status_display = dict(ContactSubmission.STATUS_CHOICES).get(old_status, old_status)
            
            html_message = render_to_string('emails/ticket_status_change.html', {
                'ticket': ticket,
                'new_status': status_display,
                'old_status': old_status_display,
                'site_name': 'DOPX',
                'site_url': site_url,
            })
            
            email = EmailMultiAlternatives(
                subject=f'Статус обращения #{str(ticket.id)[:8]} изменён | DOPX',
                body=strip_tags(html_message),
                from_email=from_email,
                to=[recipient],
            )
            email.attach_alternative(html_message, "text/html")
            email.send(fail_silently=False)
            
            logger.info(f"✅ Status change email sent to {recipient} for ticket {ticket.id}")
            
        except Exception as e:
            logger.error(f"❌ Status change email error: {type(e).__name__}: {e}", exc_info=True)
    
    actions = ['mark_as_in_progress', 'mark_as_resolved', 'mark_as_closed', export_as_csv]

    def _bulk_set_status(self, request, queryset, new_status: str) -> int:
        """
        БАГ, КОТОРЫЙ ТУТ БЫЛ: экшены ниже делали queryset.update(status=...) —
        это прямой UPDATE в БД в обход save_model()/obj.save(), поэтому
        _send_status_change_email() (см. save_model выше) никогда не
        вызывалась при массовой смене статуса из списка. Теперь идём по
        queryset поштучно и сохраняем объект как обычно — та же логика
        уведомления, что и при ручном изменении статуса в форме, только
        без чекбокса send_status_email (в bulk-экшене формы нет, письмо
        шлём всегда, если статус реально изменился).
        """
        updated = 0
        for ticket in queryset:
            old_status = ticket.status
            if old_status == new_status:
                continue
            ticket.status = new_status
            ticket.save(update_fields=['status', 'updated_at'])
            if ticket.contact_email:
                self._send_status_change_email(ticket, old_status, request)
            updated += 1
        return updated

    def mark_as_in_progress(self, request, queryset):
        updated = self._bulk_set_status(request, queryset, 'in_progress')
        self.message_user(request, f'✅ {updated} обращений взято в работу')
    mark_as_in_progress.short_description = 'Взять в работу'

    def mark_as_resolved(self, request, queryset):
        updated = self._bulk_set_status(request, queryset, 'resolved')
        self.message_user(request, f'✅ {updated} обращений решено')
    mark_as_resolved.short_description = 'Пометить как решённое'

    def mark_as_closed(self, request, queryset):
        updated = self._bulk_set_status(request, queryset, 'closed')
        self.message_user(request, f'🔒 {updated} обращений закрыто')
    mark_as_closed.short_description = 'Закрыть обращения'