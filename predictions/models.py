# predictions/models.py
"""Прогноз 1X2 до старта матча; проценты сообщества видны сразу.
Снять прогноз нельзя, только сменить выбор.
"""
from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from core.models import BaseModel


class MatchPrediction(BaseModel):
    CHOICE_HOME = '1'
    CHOICE_DRAW = 'X'
    CHOICE_AWAY = '2'
    CHOICE_CHOICES = [
        (CHOICE_HOME, _('П1 — победа хозяев')),
        (CHOICE_DRAW, _('X — ничья')),
        (CHOICE_AWAY, _('П2 — победа гостей')),
    ]

    match = models.ForeignKey(
        'matches.Match',
        on_delete=models.CASCADE,
        related_name='predictions',
        verbose_name=_('Матч'),
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='match_predictions',
        verbose_name=_('Пользователь'),
    )
    choice = models.CharField(_('Прогноз'), max_length=1, choices=CHOICE_CHOICES)

    class Meta:
        verbose_name = _('Прогноз на матч')
        verbose_name_plural = _('Прогнозы на матчи')
        constraints = [
            # Один актуальный прогноз пользователя на матч.
            models.UniqueConstraint(fields=['match', 'user'], name='unique_match_prediction'),
        ]
        indexes = [
            # Явное имя индекса.
            models.Index(fields=['match', 'choice'], name='match_prediction_choice_idx'),
        ]

    def __str__(self):
        return f"{self.user} → {self.match}: {self.choice}"

    @property
    def is_correct(self):
        """True/False после появления final_result, иначе None."""
        result = self.match.final_result
        if result is None:
            return None
        return self.choice == result
