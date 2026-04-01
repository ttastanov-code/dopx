# users/models.py
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils.translation import gettext_lazy as _
from core.models import BaseModel
from django.utils import timezone
from datetime import timedelta
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
    
    # 🔥 НОВОЕ: Настройки уведомлений в JSON поле (возвращаем!)
    _notification_settings = models.JSONField(
        _('Настройки уведомлений'),
        default=dict,
        blank=True,
        db_column='notification_settings'  # ✅ Явное имя колонки
    )
    
    # Геймификация
    total_evaluations = models.IntegerField(_('Всего оценок'), default=0)
    evaluation_streak = models.IntegerField(_('Серия оценок'), default=0)
    last_evaluation_date = models.DateField(_('Последняя оценка'), null=True, blank=True)
    
    DEFAULT_NOTIFICATION_SETTINGS = {
        'email_match_finished': True,
        'email_voting_open': True,
        'email_voting_closing': True,
        'email_top_performance': True,
        'email_system': True,
    }
    
    @property
    def notification_settings(self):
        """Безопасное получение настроек уведомлений"""
        if not hasattr(self, '_notification_settings_cache'):
            # Получаем из поля БД
            raw = self._notification_settings or {}
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except:
                    raw = {}
            # Объединяем с дефолтными настройками
            self._notification_settings_cache = {**self.DEFAULT_NOTIFICATION_SETTINGS, **raw}
        return self._notification_settings_cache
    
    @notification_settings.setter
    def notification_settings(self, value):
        """Сохранение настроек уведомлений"""
        self._notification_settings = value
        if hasattr(self, '_notification_settings_cache'):
            delattr(self, '_notification_settings_cache')
    
    def get_notification_setting(self, key, default=None):
        """Получить конкретную настройку уведомления"""
        settings = self.notification_settings
        return settings.get(key, default if default is not None else self.DEFAULT_NOTIFICATION_SETTINGS.get(key, False))
    
    def save(self, *args, **kwargs):
        """Переопределяем save для очистки кэша настроек"""
        if hasattr(self, '_notification_settings_cache'):
            delattr(self, '_notification_settings_cache')
        super().save(*args, **kwargs)
    
    def check_and_award_badges(self):
        """Проверка и выдача достижений пользователю"""
        from .models import UserBadge
        badges_to_award = []
        
        # 🔹 Первая оценка
        if self.total_evaluations >= 1:
            badges_to_award.append('first_evaluation')
        
        # 🔹 Активный фанат: 10+ оценок
        if self.total_evaluations >= 10:
            badges_to_award.append('active_fan_10')
        
        # 🔹 Хардкор фанат: 50+ оценок
        if self.total_evaluations >= 50:
            badges_to_award.append('active_fan_50')
        
        # 🔹 Серия: 7 дней подряд
        if self.evaluation_streak >= 7:
            badges_to_award.append('streak_7')
        
        # 🔹 Серия: 30 дней подряд
        if self.evaluation_streak >= 30:
            badges_to_award.append('streak_30')
        
        # 🔹 Точный аналитик: trust_score >= 1.5
        if self.trust_score >= 1.5:
            badges_to_award.append('accurate_analyst')
        
        # 🔹 Без предвзятости
        if self._check_bias_free():
            badges_to_award.append('bias_free')
        
        # Создаём достижения
        awarded = []
        for badge_type in badges_to_award:
            badge, created = UserBadge.objects.get_or_create(
                user=self,
                badge_type=badge_type
            )
            if created:
                awarded.append(badge)  # ✅ Возвращаем объект, а не строку!
        
        return awarded
    
    def _check_bias_free(self):
        """Проверка на отсутствие предвзятости"""
        from evaluations.models import ContextEvaluation, PlayerEvaluation
        from django.db.models import Avg
        
        contexts = ContextEvaluation.objects.filter(
            user=self,
            supported_team__isnull=False
        ).select_related('match').order_by('-created_at')[:10]
        
        if contexts.count() < 5:
            return False
        
        bias_scores = []
        for ctx in contexts:
            match = ctx.match
            supported = ctx.supported_team
            
            own_avg = PlayerEvaluation.objects.filter(
                user=self, match=match, player__team=supported
            ).aggregate(avg=Avg('contribution'))['avg']
            
            opponent = match.away_team if match.home_team == supported else match.home_team
            opp_avg = PlayerEvaluation.objects.filter(
                user=self, match=match, player__team=opponent
            ).aggregate(avg=Avg('contribution'))['avg']
            
            if own_avg and opp_avg:
                bias = abs(own_avg - opp_avg)
                bias_scores.append(bias)
        
        if not bias_scores:
            return False
        
        avg_bias = sum(bias_scores) / len(bias_scores)
        return avg_bias < 2.0
    
    def update_evaluation_stats(self):
        """Обновление статистики оценок + проверка достижений"""
        from django.utils import timezone
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
        
        # 🎁 Проверяем и выдаём достижения
        awarded_badges = self.check_and_award_badges()
        
        self.save(update_fields=['total_evaluations', 'evaluation_streak', 'last_evaluation_date', 'updated_at'])
        
        return awarded_badges
    
    class Meta:
        verbose_name = _('Пользователь')
        verbose_name_plural = _('Пользователи')
        ordering = ['-trust_score', '-total_evaluations']
    
    def __str__(self):
        return self.username
    
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
        """Добавить опыт и проверить повышение уровня"""
        old_level = self.level
        self.total_xp += amount
        
        # Пересчитываем уровень
        new_level = (self.total_xp // 100) + 1
        if new_level > self.level:
            self.level = new_level
            self._level_increased = True
        
        self.save()
        
        return {
            'level_increased': new_level > old_level,
            'old_level': old_level,
            'new_level': new_level,
            'xp_added': amount,
            'total_xp': self.total_xp
        }
    
    @property
    def progress_percent(self):
        """Процент прогресса до следующего уровня (0-100)"""
        return self.total_xp % 100
    
    def __str__(self):
        return f"{self.user} — Уровень {self.level}"


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
        """
        Добавить опыт и проверить повышение уровня.
        Возвращает dict с информацией об изменениях.
        """
        old_level = self.level
        old_total_xp = self.total_xp
        
        self.total_xp += amount
        
        # Пересчитываем уровень: каждый уровень = 100 XP
        new_level = (self.total_xp // 100) + 1
        
        level_increased = False
        levels_gained = []
        
        if new_level > old_level:
            # Записываем все уровни, через которые прошли
            for lvl in range(old_level + 1, new_level + 1):
                levels_gained.append(lvl)
            self.level = new_level
            level_increased = True
        
        # ✅ ВАЖНО: Сохраняем ТОЛЬКО если уровень изменился
        # Это предотвращает лишние save() при каждом +10 XP
        if level_increased:
            self.save(update_fields=['level', 'total_xp', 'updated_at'])
        else:
            # Обновляем только total_xp без level
            UserXP.objects.filter(pk=self.pk).update(
                total_xp=self.total_xp,
                updated_at=timezone.now()
            )
        
        # Возвращаем полную информацию
        return {
            'level_increased': level_increased,
            'levels_gained': levels_gained,  # [2, 3] если перескочили несколько уровней
            'old_level': old_level,
            'new_level': self.level,
            'old_total_xp': old_total_xp,
            'new_total_xp': self.total_xp,
            'xp_added': amount,
        }

    @property
    def progress_percent(self):
        """Процент прогресса до следующего уровня (0-100)"""
        xp_in_current_level = self.total_xp % 100
        return min(100, int((xp_in_current_level / 100) * 100))

    def __str__(self):
        return f"{self.user} — Уровень {self.level} ({self.total_xp} XP)"