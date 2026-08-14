# evaluations/models.py
"""
ИЗМЕНЕНИЕ: `EvaluationSession` — добавлено поле `ip_address` (антифрод,
см. `evaluations/views.py::EvaluateMatchFinalView` и продуктовый аудит,
раздел 4.3) и свойство `fill_duration_seconds`, вычисляющее, за сколько
секунд пользователь прошёл вайзард целиком — основа сигнала "слишком
быстрое заполнение — похоже на скрипт". Требует
`python manage.py makemigrations evaluations` (новая колонка).
"""
from django.db import models
from django.conf import settings
from django.core.validators import MinValueValidator, MaxValueValidator
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from core.models import BaseModel

class ContextEvaluation(BaseModel):
    """Контекст просмотра матча пользователем"""
    WATCHED_TYPE_CHOICES = [
        ('full', _('Полный матч')),
        ('highlights', _('Только голы')),
        ('partial', _('Фрагменты')),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='context_evaluations',
        verbose_name=_('Пользователь')
    )
    match = models.ForeignKey(
        'matches.Match',
        on_delete=models.CASCADE,
        related_name='context_evaluations',
        verbose_name=_('Матч')
    )
    supported_team = models.ForeignKey(
        'teams.Team',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='supported_by_users',
        verbose_name=_('Поддерживаемая команда')
    )
    watched_type = models.CharField(
        _('Тип просмотра'),
        max_length=20,
        choices=WATCHED_TYPE_CHOICES,
        default='full'
    )
    attended_stadium = models.BooleanField(_('Посещал стадион'), default=False)

    class Meta:
        verbose_name = _('Контекст оценки')
        verbose_name_plural = _('Контексты оценок')
        constraints = [
            models.UniqueConstraint(fields=['user', 'match'], name='unique_context_evaluation')
        ]
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.user.username} - {self.match}"


class TeamEvaluation(BaseModel):
    """Оценка команды пользователем"""
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='team_evaluations',
        verbose_name=_('Пользователь')
    )
    match = models.ForeignKey(
        'matches.Match',
        on_delete=models.CASCADE,
        related_name='team_evaluations',
        verbose_name=_('Матч')
    )
    team = models.ForeignKey(
        'teams.Team',
        on_delete=models.CASCADE,
        related_name='team_evaluations',
        verbose_name=_('Команда')
    )
    tactics = models.IntegerField(_('Тактика'), validators=[MinValueValidator(1), MaxValueValidator(10)])
    effort = models.IntegerField(_('Самоотдача'), validators=[MinValueValidator(1), MaxValueValidator(10)])
    organization = models.IntegerField(_('Организация'), validators=[MinValueValidator(1), MaxValueValidator(10)])
    mentality = models.IntegerField(_('Менталитет'), validators=[MinValueValidator(1), MaxValueValidator(10)])

    class Meta:
        verbose_name = _('Оценка команды')
        verbose_name_plural = _('Оценки команд')
        constraints = [
            models.UniqueConstraint(fields=['user', 'match', 'team'], name='unique_team_evaluation')
        ]
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.user.username} - {self.team} ({self.match})"

    @property
    def average_score(self):
        return (self.tactics + self.effort + self.organization + self.mentality) / 4


class PlayerEvaluation(BaseModel):
    """Оценка игрока пользователем"""
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='player_evaluations',
        verbose_name=_('Пользователь')
    )
    match = models.ForeignKey(
        'matches.Match',
        on_delete=models.CASCADE,
        related_name='player_evaluations',
        verbose_name=_('Матч')
    )
    player = models.ForeignKey(
        'players.Player',
        on_delete=models.CASCADE,
        related_name='player_evaluations',
        verbose_name=_('Игрок')
    )
    contribution = models.IntegerField(
        _('Вклад'),
        validators=[MinValueValidator(1), MaxValueValidator(10)],
        help_text=_("Вклад в игру (1-10)")
    )
    risk = models.IntegerField(
        _('Риск'),
        validators=[MinValueValidator(1), MaxValueValidator(10)],
        help_text=_("Риск/ошибки (1-10)")
    )
    potential = models.IntegerField(
        _('Потенциал'),
        validators=[MinValueValidator(1), MaxValueValidator(10)],
        help_text=_("Потенциал (1-10)")
    )

    class Meta:
        verbose_name = _('Оценка игрока')
        verbose_name_plural = _('Оценки игроков')
        constraints = [
            models.UniqueConstraint(fields=['user', 'match', 'player'], name='unique_player_evaluation')
        ]
        indexes = [
            models.Index(fields=['match', 'player']),
            models.Index(fields=['player', 'match']),
            models.Index(fields=['user', 'match']),
        ]
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.user.username} - {self.player} ({self.match})"

    @property
    def maturity_score(self):
        return self.contribution - self.risk


class CoachEvaluation(BaseModel):
    """Оценка тренера пользователем"""
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='coach_evaluations',
        verbose_name=_('Пользователь')
    )
    match = models.ForeignKey(
        'matches.Match',
        on_delete=models.CASCADE,
        related_name='coach_evaluations',
        verbose_name=_('Матч')
    )
    coach = models.ForeignKey(
        'coaches.Coach',
        on_delete=models.CASCADE,
        related_name='coach_evaluations',
        verbose_name=_('Тренер')
    )
    tactics = models.IntegerField(_('Тактика'), validators=[MinValueValidator(1), MaxValueValidator(10)])
    substitutions = models.IntegerField(_('Замены'), validators=[MinValueValidator(1), MaxValueValidator(10)])
    game_management = models.IntegerField(_('Управление'), validators=[MinValueValidator(1), MaxValueValidator(10)])
    impact = models.IntegerField(_('Влияние'), validators=[MinValueValidator(1), MaxValueValidator(10)])

    class Meta:
        verbose_name = _('Оценка тренера')
        verbose_name_plural = _('Оценки тренеров')
        constraints = [
            models.UniqueConstraint(fields=['user', 'match', 'coach'], name='unique_coach_evaluation')
        ]
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.user.username} - {self.coach} ({self.match})"

    @property
    def average_score(self):
        return (self.tactics + self.substitutions + self.game_management + self.impact) / 4


class RefereeEvaluation(BaseModel):
    """Оценка судейства пользователем"""
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='referee_evaluations',
        verbose_name=_('Пользователь')
    )
    match = models.ForeignKey(
        'matches.Match',
        on_delete=models.CASCADE,
        related_name='referee_evaluations',
        verbose_name=_('Матч')
    )
    influence_score = models.IntegerField(
        _('Влияние на матч'),
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text=_("Влияние на матч (0-100)")
    )
    decision_quality = models.IntegerField(
        _('Качество решений'),
        validators=[MinValueValidator(1), MaxValueValidator(10)],
        help_text=_("Качество решений (1-10)")
    )

    class Meta:
        verbose_name = _('Оценка судейства')
        verbose_name_plural = _('Оценки судейства')
        constraints = [
            models.UniqueConstraint(fields=['user', 'match'], name='unique_referee_evaluation')
        ]
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.user.username} - Судья ({self.match})"


class MatchEvaluation(BaseModel):
    """Общая оценка матча пользователем"""
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='match_evaluations',
        verbose_name=_('Пользователь')
    )
    match = models.ForeignKey(
        'matches.Match',
        on_delete=models.CASCADE,
        related_name='match_evaluations',
        verbose_name=_('Матч')
    )
    entertainment = models.IntegerField(_('Зрелищность'), validators=[MinValueValidator(1), MaxValueValidator(10)])
    tension = models.IntegerField(_('Напряжение'), validators=[MinValueValidator(1), MaxValueValidator(10)])
    turning_point = models.BooleanField(_('Переломный момент'), default=False)
    fairness = models.IntegerField(_('Справедливость'), validators=[MinValueValidator(1), MaxValueValidator(10)])

    class Meta:
        verbose_name = _('Оценка матча')
        verbose_name_plural = _('Оценки матчей')
        constraints = [
            models.UniqueConstraint(fields=['user', 'match'], name='unique_match_evaluation')
        ]
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.user.username} - Матч ({self.match})"

    @property
    def drama_index(self):
        return self.entertainment * self.tension


class EvaluationSession(BaseModel):
    """Отслеживание прогресса оценки пользователя"""
    STATUS_CHOICES = [
        ('started', _('Начато')),
        ('in_progress', _('В процессе')),
        ('completed', _('Завершено')),
        ('abandoned', _('Заброшено')),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='evaluation_sessions',
        verbose_name=_('Пользователь')
    )
    match = models.ForeignKey(
        'matches.Match',
        on_delete=models.CASCADE,
        related_name='evaluation_sessions',
        verbose_name=_('Матч')
    )
    status = models.CharField(
        _('Статус'),
        max_length=20,
        choices=STATUS_CHOICES,
        default='started'
    )
    completed_steps = models.JSONField(_('Завершённые шаги'), default=list)
    current_step = models.CharField(_('Текущий шаг'), max_length=50, default='context')
    started_at = models.DateTimeField(_('Начато'), auto_now_add=True)
    completed_at = models.DateTimeField(_('Завершено'), null=True, blank=True)
    # НОВОЕ (антифрод): IP, с которого была ЗАВЕРШЕНА сессия оценки —
    # см. evaluations/views.py::EvaluateMatchFinalView и продуктовый аудит.
    ip_address = models.GenericIPAddressField(_('IP адрес завершения'), null=True, blank=True)

    class Meta:
        verbose_name = _('Сессия оценки')
        verbose_name_plural = _('Сессии оценок')
        constraints = [
            models.UniqueConstraint(fields=['user', 'match'], name='unique_evaluation_session')
        ]
        indexes = [
            models.Index(fields=['user', 'status']),
            models.Index(fields=['match', 'status']),
        ]
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.user} - {self.match} ({self.status})"

    def progress_percentage(self):
        total_steps = 6
        completed = len(self.completed_steps)
        return int((completed / total_steps) * 100)

    def next_step_url(self, match_id):
        steps = ['context', 'teams', 'players', 'coaches', 'referee', 'match_eval', 'complete']
        next_idx = len(self.completed_steps) + 1
        if next_idx >= len(steps):
            return None
        step_name = steps[next_idx]
        if step_name == 'complete':
            return f'/evaluations/complete/{match_id}/'
        return f'/evaluations/match/{match_id}/{step_name}/'

    @property
    def fill_duration_seconds(self) -> float | None:
        """
        Сколько секунд заняло прохождение вайзарда целиком (от `started_at`
        до `completed_at`). `None`, если сессия ещё не завершена.

        Используется как антифрод-сигнал: физически невозможно осмысленно
        заполнить 6 шагов (контекст, команды, до 22+ игроков, тренеры, судья,
        финал) за считаные секунды — см.
        `evaluations/views.py::EvaluateMatchFinalView._flag_if_suspicious`.
        """
        if not self.completed_at:
            return None
        return (self.completed_at - self.started_at).total_seconds()