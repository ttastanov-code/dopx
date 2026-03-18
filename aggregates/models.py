# aggregates/models.py
from django.db import models
from django.db.models import Avg, StdDev, Count
from core.models import BaseModel
from evaluations.models import PlayerEvaluation, CoachEvaluation, MatchEvaluation


class PlayerMatchAggregate(BaseModel):
    """Агрегированные оценки игрока за матч"""
    player = models.ForeignKey(
        'players.Player',
        on_delete=models.CASCADE,
        related_name='match_aggregates'
    )
    match = models.ForeignKey(
        'matches.Match',
        on_delete=models.CASCADE,
        related_name='player_aggregates'
    )
    
    # Базовые метрики
    avg_contribution = models.FloatField(default=0.0)
    avg_risk = models.FloatField(default=0.0)
    avg_potential = models.FloatField(default=0.0)
    total_votes = models.IntegerField(default=0)
    
    # Вычисляемые индексы
    performance_score = models.FloatField(default=0.0)  # avg_contribution
    risk_index = models.FloatField(default=0.0)  # avg_risk
    maturity_score = models.FloatField(default=0.0)  # contribution - risk
    stability_index = models.FloatField(default=0.0)  # 1 / std_dev(contribution)
    clutch_index = models.FloatField(default=0.0)  # contribution * drama_index
    
    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['player', 'match'],
                name='unique_player_match_aggregate'
            )
        ]
        indexes = [
            models.Index(fields=['player', 'match']),
            models.Index(fields=['match', 'player']),
        ]
    
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
        related_name='match_aggregates'
    )
    match = models.ForeignKey(
        'matches.Match',
        on_delete=models.CASCADE,
        related_name='coach_aggregates'
    )
    
    avg_tactics = models.FloatField(default=0.0)
    avg_substitutions = models.FloatField(default=0.0)
    avg_management = models.FloatField(default=0.0)
    avg_impact = models.FloatField(default=0.0)
    total_votes = models.IntegerField(default=0)
    
    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['coach', 'match'],
                name='unique_coach_match_aggregate'
            )
        ]
        indexes = [
            models.Index(fields=['coach', 'match']),
        ]
    
    def __str__(self):
        return f"{self.coach} - {self.match}"
    
    @property
    def average_score(self):
        if self.total_votes == 0:
            return 0.0
        return (self.avg_tactics + self.avg_substitutions + 
                self.avg_management + self.avg_impact) / 4


class MatchAggregate(BaseModel):
    """Агрегированные оценки матча"""
    match = models.OneToOneField(
        'matches.Match',
        on_delete=models.CASCADE,
        related_name='aggregate'
    )
    
    avg_entertainment = models.FloatField(default=0.0)
    avg_tension = models.FloatField(default=0.0)
    avg_fairness = models.FloatField(default=0.0)
    turning_point_ratio = models.FloatField(default=0.0)
    total_votes = models.IntegerField(default=0)
    
    # Вычисляемые индексы
    drama_index = models.FloatField(default=0.0)  # entertainment * tension
    
    def __str__(self):
        return f"Aggregate - {self.match}"
    
    def calculate_drama_index(self):
        """Drama Index = entertainment * tension"""
        return self.avg_entertainment * self.avg_tension