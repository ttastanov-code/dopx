# aggregates/models.py
from django.db import models
from django.utils.translation import gettext_lazy as _
from core.models import BaseModel

class PlayerMatchAggregate(BaseModel):
    """Агрегированные оценки игрока за матч"""
    player = models.ForeignKey(
        'players.Player',
        on_delete=models.CASCADE,
        related_name='match_aggregates',
        verbose_name=_('Игрок')
    )
    match = models.ForeignKey(
        'matches.Match',
        on_delete=models.CASCADE,
        related_name='player_aggregates',
        verbose_name=_('Матч')
    )
    
    # Базовые метрики
    avg_contribution = models.FloatField(_('Средний вклад'), default=0.0)
    avg_risk = models.FloatField(_('Средний риск'), default=0.0)
    avg_potential = models.FloatField(_('Средний потенциал'), default=0.0)
    total_votes = models.IntegerField(_('Всего голосов'), default=0)
    
    # Вычисляемые индексы
    performance_score = models.FloatField(_('Оценка выступления'), default=0.0)
    risk_index = models.FloatField(_('Индекс риска'), default=0.0)
    maturity_score = models.FloatField(_('Индекс зрелости'), default=0.0)
    stability_index = models.FloatField(_('Индекс стабильности'), default=0.0)
    clutch_index = models.FloatField(_('Индекс решающих моментов'), default=0.0)
    
    class Meta:
        verbose_name = _('Агрегат игрока')
        verbose_name_plural = _('Агрегаты игроков')
        constraints = [
            models.UniqueConstraint(fields=['player', 'match'], name='unique_player_match_aggregate')
        ]
        indexes = [
            models.Index(fields=['player', 'match']),
            models.Index(fields=['match', 'player']),
            models.Index(fields=['-performance_score']),
            models.Index(fields=['match', '-performance_score']),
        ]
        ordering = ['-performance_score']
    
    def __str__(self):
        return f"{self.player} - {self.match}"
    
    @property
    def potential_index(self):
        return self.avg_potential


class CoachMatchAggregate(BaseModel):
    """Агрегированные оценки тренера за матч"""
    coach = models.ForeignKey(
        'coaches.Coach',
        on_delete=models.CASCADE,
        related_name='match_aggregates',
        verbose_name=_('Тренер')
    )
    match = models.ForeignKey(
        'matches.Match',
        on_delete=models.CASCADE,
        related_name='coach_aggregates',
        verbose_name=_('Матч')
    )
    avg_tactics = models.FloatField(_('Средняя тактика'), default=0.0)
    avg_substitutions = models.FloatField(_('Средние замены'), default=0.0)
    avg_management = models.FloatField(_('Среднее управление'), default=0.0)
    avg_impact = models.FloatField(_('Среднее влияние'), default=0.0)
    total_votes = models.IntegerField(_('Всего голосов'), default=0)
    
    class Meta:
        verbose_name = _('Агрегат тренера')
        verbose_name_plural = _('Агрегаты тренеров')
        constraints = [
            models.UniqueConstraint(fields=['coach', 'match'], name='unique_coach_match_aggregate')
        ]
        indexes = [
            models.Index(fields=['coach', 'match']),
        ]
        ordering = ['-match__start_time']
    
    def __str__(self):
        return f"{self.coach} - {self.match}"
    
    @property
    def average_score(self):
        if self.total_votes == 0:
            return 0.0
        return (self.avg_tactics + self.avg_substitutions + self.avg_management + self.avg_impact) / 4


class MatchAggregate(BaseModel):
    """Агрегированные оценки матча"""
    match = models.OneToOneField(
        'matches.Match',
        on_delete=models.CASCADE,
        related_name='aggregate',
        verbose_name=_('Матч')
    )
    avg_entertainment = models.FloatField(_('Средняя зрелищность'), default=0.0)
    avg_tension = models.FloatField(_('Среднее напряжение'), default=0.0)
    avg_fairness = models.FloatField(_('Средняя справедливость'), default=0.0)
    turning_point_ratio = models.FloatField(_('Доля переломных моментов'), default=0.0)
    total_votes = models.IntegerField(_('Всего голосов'), default=0)
    drama_index = models.FloatField(_('Индекс драмы'), default=0.0)
    
    class Meta:
        verbose_name = _('Агрегат матча')
        verbose_name_plural = _('Агрегаты матчей')
        indexes = [
            models.Index(fields=['match']),
        ]
        ordering = ['-match__start_time']
    
    def __str__(self):
        return f"Агрегат - {self.match}"
    
    def calculate_drama_index(self):
        return self.avg_entertainment * self.avg_tension