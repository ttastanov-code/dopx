# notifications/models.py
from django.db import models
from django.conf import settings
from django.utils.translation import gettext_lazy as _
from core.models import BaseModel


class Notification(BaseModel):
    """Модель уведомлений пользователей"""
    
    NOTIFICATION_TYPES = [
        ('match_finished', _('Матч завершён')),
        ('voting_open', _('Голосование открыто')),
        ('voting_closing', _('Голосование закрывается')),
        ('aggregate_updated', _('Агрегаты обновлены')),
        ('top_performance', _('Вы в топ-3 матча')),
        ('verification_required', _('Требуется верификация')),
        ('new_badge', _('Новое достижение')),
        ('level_up', _('Новый уровень')),
        ('system', _('Системное')),
    ]
    
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='notifications',
        verbose_name=_('Пользователь')
    )
    
    notification_type = models.CharField(
        _('Тип уведомления'),
        max_length=30,
        choices=NOTIFICATION_TYPES,
        default='system'
    )
    
    title = models.CharField(_('Заголовок'), max_length=255)
    message = models.TextField(_('Сообщение'))
    is_read = models.BooleanField(_('Прочитано'), default=False)
    action_url = models.URLField(_('URL действия'), blank=True, null=True)
    
    related_match = models.ForeignKey(
        'matches.Match',
        on_delete=models.CASCADE,
        null=True, 
        blank=True,
        related_name='notifications',
        verbose_name=_('Матч')
    )

    class Meta:
        verbose_name = _('Уведомление')
        verbose_name_plural = _('Уведомления')
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', '-created_at']),
            models.Index(fields=['user', 'is_read']),
        ]

    def __str__(self):
        return f"{self.user.username} - {self.title}"