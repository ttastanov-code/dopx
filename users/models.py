# users/models.py
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils.translation import gettext_lazy as _
from core.models import BaseModel
from django.utils import timezone
from datetime import timedelta

class User(AbstractUser, BaseModel):
    """Пользователь платформы"""
    email = models.EmailField(_('Email'), unique=True)
    avatar = models.ImageField(_('Аватар'), upload_to="avatars/", null=True, blank=True)
    bio = models.TextField(_('О себе'), blank=True)
    city = models.CharField(_('Город'), max_length=120, blank=True)
    rating_power = models.FloatField(_('Сила рейтинга'), default=1.0)
    trust_score = models.FloatField(_('Оценка доверия'), default=1.0)
    is_verified = models.BooleanField(_('Верифицирован'), default=False)
    
    # Геймификация
    total_evaluations = models.IntegerField(_('Всего оценок'), default=0)
    evaluation_streak = models.IntegerField(_('Серия оценок'), default=0)
    last_evaluation_date = models.DateField(_('Последняя оценка'), null=True, blank=True)
    
    class Meta:
        verbose_name = _('Пользователь')
        verbose_name_plural = _('Пользователи')
        ordering = ['-trust_score', '-total_evaluations']
    
    def __str__(self):
        return self.username
    
    def update_evaluation_stats(self):
        """Обновление статистики оценок пользователя"""
        today = timezone.now().date()
        self.total_evaluations += 1
        
        if self.last_evaluation_date:
            days_diff = (today - self.last_evaluation_date).days
            if days_diff == 1:
                self.evaluation_streak += 1
            elif days_diff > 1:
                self.evaluation_streak = 1
        else:
            self.evaluation_streak = 1
        
        self.last_evaluation_date = today
        self.save(update_fields=['total_evaluations', 'evaluation_streak', 'last_evaluation_date', 'updated_at'])
    
    def get_trust_level(self):
        """Уровень доверия"""
        if self.trust_score >= 1.8:
            return 'expert', _('Эксперт')
        elif self.trust_score >= 1.4:
            return 'reliable', _('Надёжный')
        elif self.trust_score >= 1.0:
            return 'standard', _('Стандартный')
        else:
            return 'new', _('Новичок')
    
    def get_bias_score(self, match=None):
        """Расчёт предвзятости пользователя"""
        from evaluations.models import ContextEvaluation, PlayerEvaluation
        from django.db.models import Avg
        
        if not match:
            return 0.0
        
        context = ContextEvaluation.objects.filter(user=self, match=match).first()
        if not context or not context.supported_team:
            return 0.0
        
        own_team_evals = PlayerEvaluation.objects.filter(
            user=self, match=match, player__team=context.supported_team
        ).aggregate(avg=Avg('contribution'))['avg'] or 0
        
        opponent_team = match.away_team if match.home_team == context.supported_team else match.home_team
        opponent_evals = PlayerEvaluation.objects.filter(
            user=self, match=match, player__team=opponent_team
        ).aggregate(avg=Avg('contribution'))['avg'] or 0
        
        return own_team_evals - opponent_evals
    
    @property
    def unread_notifications_count(self):
        """Количество непрочитанных уведомлений"""
        return self.notifications.filter(is_read=False).count()


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
    
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='badges',
        verbose_name=_('Пользователь')
    )
    badge_type = models.CharField(_('Тип достижения'), max_length=50, choices=BADGE_TYPES)
    awarded_at = models.DateTimeField(_('Дата получения'), auto_now_add=True)
    
    class Meta:
        verbose_name = _('Достижение')
        verbose_name_plural = _('Достижения')
        constraints = [
            models.UniqueConstraint(fields=['user', 'badge_type'], name='unique_user_badge')
        ]
        ordering = ['-awarded_at']
    
    def __str__(self):
        return f"{self.user} - {self.get_badge_type_display()}"


class UserXP(BaseModel):
    """Опыт и рейтинг пользователя"""
    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name='xp',
        verbose_name=_('Пользователь')
    )
    total_xp = models.IntegerField(_('Всего опыта'), default=0)
    level = models.IntegerField(_('Уровень'), default=1)
    
    class Meta:
        verbose_name = _('Опыт пользователя')
        verbose_name_plural = _('Опыт пользователей')
    
    def add_xp(self, amount):
        """Добавить опыт"""
        self.total_xp += amount
        new_level = (self.total_xp // 100) + 1
        if new_level > self.level:
            self.level = new_level
        self.save()
    
    def __str__(self):
        return f"{self.user} — Уровень {self.level}"