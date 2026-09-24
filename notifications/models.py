# notifications/models.py
"""Уведомления и обращения пользователей.
email_sent_at — письмо уже ушло (мгновенно или дайджестом); null у старых записей = обработано.
"""
from django.db import models
from django.conf import settings
from django.utils.translation import gettext_lazy as _
from core.models import BaseModel

class Notification(BaseModel):
    """Уведомление пользователя."""
    NOTIFICATION_TYPES = [
        ('welcome', _('Приветственное письмо')),
        ('match_finished', _('Матч завершён / Голосование открыто')),
        ('voting_open', _('Голосование открыто')),
        ('voting_closing', _('Напоминание о закрытии голосования')),
        ('new_badge', _('Новое достижение')),
        ('level_up', _('Повышение уровня')),
        ('aggregate_updated', _('Обновление рейтинга')),
        ('top_performance', _('Топ-выступление')),
        ('verification_required', _('Требуется подтверждение email')),
        ('system', _('Системное уведомление')),
        # Retention-уведомления (notifications/tasks.py).
        ('prediction_closing', _('Скоро закроется приём прогнозов')),
        ('weekly_digest', _('Персональная сводка недели')),
        ('prediction_result', _('Прогноз vs результат матча')),
        # Итоги «DOPX Лучшие тура».
        ('round_results', _('Итоги «DOPX Лучшие тура»')),
        # Live-события матча для подписчиков (может быть несколько за матч).
        ('match_event', _('Live-событие матча')),
        # «Матч начался» и «Составы объявлены».
        ('match_started', _('Матч начался')),
        ('lineups_available', _('Составы объявлены')),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='notifications', verbose_name=_('Пользователь')
    )
    notification_type = models.CharField(
        _('Тип уведомления'), max_length=30, choices=NOTIFICATION_TYPES, default='system'
    )
    title = models.CharField(_('Заголовок'), max_length=255)
    message = models.TextField(_('Сообщение'))
    is_read = models.BooleanField(_('Прочитано'), default=False)
    action_url = models.URLField(_('URL действия'), blank=True, null=True)
    related_match = models.ForeignKey(
        'matches.Match', on_delete=models.CASCADE, null=True, blank=True, related_name='notifications', verbose_name=_('Матч')
    )
    # См. докстринг модуля.
    email_sent_at = models.DateTimeField(_('Email отправлен'), null=True, blank=True)

    class Meta:
        verbose_name = _('Уведомление')
        verbose_name_plural = _('Уведомления')
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', '-created_at']),
            models.Index(fields=['user', 'is_read']),
            models.Index(fields=['email_sent_at']),
        ]

    def __str__(self):
        return f"{self.user.username} - {self.title}"


class ContactSubmission(BaseModel):
    """Обращение пользователя."""
    STATUS_CHOICES = [
        ('new', _('Новое')),
        ('in_progress', _('В работе')),
        ('resolved', _('Решено')),
        ('closed', _('Закрыто')),
    ]
    CATEGORY_CHOICES = [
        ('general', _('Общий вопрос')),
        ('bug', _('Сообщение об ошибке')),
        ('feature', _('Предложение функции')),
        ('evaluation', _('Проблема с оценкой матча')),
        ('account', _('Вопрос по аккаунту')),
        # «Право на ответ» — отдельная категория для фильтра.
        ('dispute', _('Оспорить рейтинг / право на ответ')),
        # Ошибка в данных матча — отдельная очередь в «Центре доверия к данным».
        ('data_error', _('Ошибка в данных матча')),
        ('other', _('Другое')),
    ]
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='contact_submissions', verbose_name=_('Пользователь'))
    guest_email = models.EmailField(_('Email гостя'), blank=True, help_text=_('Заполняется если пользователь не авторизован'))
    category = models.CharField(_('Категория'), max_length=30, choices=CATEGORY_CHOICES, default='general')
    subject = models.CharField(_('Тема'), max_length=255)
    message = models.TextField(_('Сообщение'))
    attachment = models.FileField(_('Вложение'), upload_to='contact_attachments/', null=True, blank=True, help_text=_('Скриншот или документ (макс. 5MB)'))
    status = models.CharField(_('Статус'), max_length=20, choices=STATUS_CHOICES, default='new')
    admin_response = models.TextField(_('Ответ админа'), blank=True, help_text=_('Внутренний ответ для истории'))
    ip_address = models.GenericIPAddressField(_('IP адрес'), null=True, blank=True)
    user_agent = models.TextField(_('User Agent'), blank=True)
    # Матч, к которому относится жалоба (SET_NULL).
    related_match = models.ForeignKey(
        'matches.Match', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='data_error_reports', verbose_name=_('Матч (если жалоба на данные)'),
    )

    class Meta:
        verbose_name = _('Обращение')
        verbose_name_plural = _('Обращения')
        ordering = ['-created_at']

    def __str__(self):
        return f"#{str(self.id)[:8]} - {self.subject} ({self.get_status_display()})"

    @property
    def contact_email(self):
        if self.user and self.user.email:
            return self.user.email
        return self.guest_email