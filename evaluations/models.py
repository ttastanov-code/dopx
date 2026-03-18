from django.db import models
from django.conf import settings
from django.core.validators import MinValueValidator, MaxValueValidator
from django.utils import timezone
from core.models import BaseModel


class ContextEvaluation(BaseModel):
    """
    Контекст просмотра матча пользователем.
    Первый шаг оценки — собираем контекст перед основными оценками.
    """
    WATCHED_TYPE_CHOICES = [
        ('full', 'Full Match'),
        ('highlights', 'Highlights'),
        ('partial', 'Partial'),
    ]
    
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='context_evaluations'
    )
    match = models.ForeignKey(
        'matches.Match',
        on_delete=models.CASCADE,
        related_name='context_evaluations'
    )
    supported_team = models.ForeignKey(
        'teams.Team',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='supported_by_users'
    )
    watched_type = models.CharField(
        max_length=20,
        choices=WATCHED_TYPE_CHOICES,
        default='full'
    )
    attended_stadium = models.BooleanField(default=False)
    
    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'match'],
                name='unique_context_evaluation'
            )
        ]
    
    def __str__(self):
        return f"{self.user.username} - {self.match}"


class TeamEvaluation(BaseModel):
    """
    Оценка команды пользователем (4 оси).
    """
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='team_evaluations'
    )
    match = models.ForeignKey(
        'matches.Match',
        on_delete=models.CASCADE,
        related_name='team_evaluations'
    )
    team = models.ForeignKey(
        'teams.Team',
        on_delete=models.CASCADE,
        related_name='team_evaluations'
    )
    
    tactics = models.IntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(10)]
    )
    effort = models.IntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(10)]
    )
    organization = models.IntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(10)]
    )
    mentality = models.IntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(10)]
    )
    
    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'match', 'team'],
                name='unique_team_evaluation'
            )
        ]
    
    def __str__(self):
        return f"{self.user.username} - {self.team} ({self.match})"
    
    @property
    def average_score(self):
        """Средний балл по всем осям"""
        return (self.tactics + self.effort + self.organization + self.mentality) / 4


class PlayerEvaluation(BaseModel):
    """
    Оценка игрока пользователем (3 оси — ключевая модель).
    """
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='player_evaluations'
    )
    match = models.ForeignKey(
        'matches.Match',
        on_delete=models.CASCADE,
        related_name='player_evaluations'
    )
    player = models.ForeignKey(
        'players.Player',
        on_delete=models.CASCADE,
        related_name='player_evaluations'
    )
    
    contribution = models.IntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(10)],
        help_text="Вклад в игру (1-10)"
    )
    risk = models.IntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(10)],
        help_text="Риск/ошибки (1-10)"
    )
    potential = models.IntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(10)],
        help_text="Потенциал (1-10)"
    )
    
    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'match', 'player'],
                name='unique_player_evaluation'
            )
        ]
        indexes = [
            models.Index(fields=['match', 'player']),
            models.Index(fields=['player', 'match']),
        ]
    
    def __str__(self):
        return f"{self.user.username} - {self.player} ({self.match})"
    
    @property
    def maturity_score(self):
        """Maturity Score = contribution - risk"""
        return self.contribution - self.risk


class CoachEvaluation(BaseModel):
    """
    Оценка тренера пользователем (4 оси).
    """
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='coach_evaluations'
    )
    match = models.ForeignKey(
        'matches.Match',
        on_delete=models.CASCADE,
        related_name='coach_evaluations'
    )
    coach = models.ForeignKey(
        'coaches.Coach',
        on_delete=models.CASCADE,
        related_name='coach_evaluations'
    )
    
    tactics = models.IntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(10)]
    )
    substitutions = models.IntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(10)]
    )
    game_management = models.IntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(10)]
    )
    impact = models.IntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(10)]
    )
    
    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'match', 'coach'],
                name='unique_coach_evaluation'
            )
        ]
    
    def __str__(self):
        return f"{self.user.username} - {self.coach} ({self.match})"
    
    @property
    def average_score(self):
        return (self.tactics + self.substitutions + self.game_management + self.impact) / 4


class RefereeEvaluation(BaseModel):
    """
    Оценка судейства пользователем.
    """
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='referee_evaluations'
    )
    match = models.ForeignKey(
        'matches.Match',
        on_delete=models.CASCADE,
        related_name='referee_evaluations'
    )
    
    influence_score = models.IntegerField(
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text="Влияние на матч (0-100)"
    )
    decision_quality = models.IntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(10)],
        help_text="Качество решений (1-10)"
    )
    
    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'match'],
                name='unique_referee_evaluation'
            )
        ]
    
    def __str__(self):
        return f"{self.user.username} - Referee ({self.match})"


class MatchEvaluation(BaseModel):
    """
    Общая оценка матча пользователем.
    """
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='match_evaluations'
    )
    match = models.ForeignKey(
        'matches.Match',
        on_delete=models.CASCADE,
        related_name='match_evaluations'
    )
    
    entertainment = models.IntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(10)]
    )
    tension = models.IntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(10)]
    )
    turning_point = models.BooleanField(default=False)
    fairness = models.IntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(10)]
    )
    
    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'match'],
                name='unique_match_evaluation'
            )
        ]
    
    def __str__(self):
        return f"{self.user.username} - Match ({self.match})"
    
    @property
    def drama_index(self):
        """Drama Index = entertainment * tension"""
        return self.entertainment * self.tension