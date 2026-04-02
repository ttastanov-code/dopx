# users/models.py
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils.translation import gettext_lazy as _
from core.models import BaseModel
from django.utils import timezone
import uuid
import json


class User(AbstractUser, BaseModel):
    """Пользователь платформы"""
    email = models.EmailField(_('Email'), unique=True)
    avatar = models.ImageField(_('Аватар'), upload_to="avatars/", null=True, blank=True)
    bio = models.TextField(_('О себе'), blank=True)
    city = models.CharField(_('Город'), max_length=120, blank=True)
    rating_power = models.FloatField(_('Сила рейтинга'), default=1.0)
    trust_score = models.FloatField(_('Оценка доверия'), default=1.0)
    is_verified = models.BooleanField(_('Верифицирован'), default=False)
    verification_token = models.UUIDField(_('Токен верификации'), default=uuid.uuid4, editable=False, null=True, blank=True)
    verification_token_created_at = models.DateTimeField(_('Дата создания токена'), auto_now_add=True)
    
    _notification_settings = models.JSONField(
        _('Настройки уведомлений'), default=dict, blank=True, db_column='notification_settings'
    )
    
    total_evaluations = models.IntegerField(_('Всего оценок'), default=0)
    evaluation_streak = models.IntegerField(_('Серия оценок'), default=0)
    last_evaluation_date = models.DateField(_('Последняя оценка'), null=True, blank=True)

    DEFAULT_NOTIFICATION_SETTINGS = {
        'email_match_finished': True,
        'email_voting_closing': True,
        'email_new_badge': True,
        'email_level_up': True,
        'email_system': True,
    }

    @property
    def notification_settings(self):
        raw = self._notification_settings or {}
        if isinstance(raw, str):
            try: raw = json.loads(raw)
            except: raw = {}
        if not isinstance(raw, dict): raw = {}
        return {**self.DEFAULT_NOTIFICATION_SETTINGS, **raw}

    @notification_settings.setter
    def notification_settings(self, value):
        self._notification_settings = value

    def get_notification_setting(self, key, default=None):
        """Безопасное получение настройки уведомления"""
        return self.notification_settings.get(
            key, default if default is not None else self.DEFAULT_NOTIFICATION_SETTINGS.get(key, False)
        )

    def update_evaluation_stats(self):
        """Обновляет статистику оценок. Достижения проверяются отдельно."""
        today = timezone.now().date()
        self.total_evaluations += 1
        if self.last_evaluation_date:
            days_diff = (today - self.last_evaluation_date).days
            if days_diff == 1:
                self.evaluation_streak += 1
            elif days_diff > 1:
                self.evaluation_streak = 1
            else:
                # Оценка в тот же день - не меняем серию
                pass
        else:
            self.evaluation_streak = 1
        self.last_evaluation_date = today
        self.save(update_fields=['total_evaluations', 'evaluation_streak', 'last_evaluation_date', 'updated_at'])

    def get_trust_level(self):
        if self.trust_score >= 1.8: return 'expert', _('Эксперт')
        elif self.trust_score >= 1.4: return 'reliable', _('Надёжный')
        elif self.trust_score >= 1.0: return 'standard', _('Стандартный')
        else: return 'new', _('Новичок')

    @property
    def unread_notifications_count(self):
        return self.notifications.filter(is_read=False).count()

    class Meta:
        verbose_name = _('Пользователь')
        verbose_name_plural = _('Пользователи')
        ordering = ['-trust_score', '-total_evaluations']

    def __str__(self):
        return self.username


class UserBadge(BaseModel):
    """Достижения пользователей"""
    BADGE_TYPES = [
        ('first_evaluation', _('Первая оценка')),
        ('active_fan_10', _('Активный фанат (10 матчей)')),
        ('active_fan_50', _('Хардкор фанат (50 матчей)')),
        ('accurate_analyst', _('Точный аналитик')),
        ('bias_free', _('Без предвзятости')),
        ('early_bird', _('Ранняя пташка')),
        ('streak_7', _('Неделя подряд')),
        ('streak_30', _('Месяц подряд')),
    ]
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='badges', verbose_name=_('Пользователь'))
    badge_type = models.CharField(_('Тип достижения'), max_length=50, choices=BADGE_TYPES)
    awarded_at = models.DateTimeField(_('Дата получения'), auto_now_add=True)

    class Meta:
        verbose_name = _('Достижение')
        verbose_name_plural = _('Достижения')
        constraints = [models.UniqueConstraint(fields=['user', 'badge_type'], name='unique_user_badge')]
        ordering = ['-awarded_at']

    def __str__(self):
        return f"{self.user} - {self.get_badge_type_display()}"


class UserXP(BaseModel):
    """Опыт и рейтинг пользователя"""
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='xp', verbose_name=_('Пользователь'))
    total_xp = models.IntegerField(_('Всего опыта'), default=0)
    level = models.IntegerField(_('Уровень'), default=1)

    class Meta:
        verbose_name = _('Опыт пользователя')
        verbose_name_plural = _('Опыт пользователей')

    def add_xp(self, amount):
        old_level = self.level
        old_total_xp = self.total_xp
        
        self.total_xp += amount
        new_level = (self.total_xp // 100) + 1
        
        level_increased = False
        levels_gained = []
        
        if new_level > old_level:
            for lvl in range(old_level + 1, new_level + 1):
                levels_gained.append(lvl)
            self.level = new_level
            level_increased = True
            
        self.save(update_fields=['level', 'total_xp', 'updated_at'])
        
        return {
            'level_increased': level_increased,
            'levels_gained': levels_gained,
            'old_level': old_level,
            'new_level': self.level,
            'old_total_xp': old_total_xp,
            'new_total_xp': self.total_xp,
            'xp_added': amount,
        }

    @property
    def progress_percent(self):
        return min(100, int(((self.total_xp % 100) / 100) * 100))

    def __str__(self):
        return f"{self.user} — Уровень {self.level} ({self.total_xp} XP)"