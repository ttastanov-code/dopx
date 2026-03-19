# seasons/models.py
from django.db import models
from django.utils.translation import gettext_lazy as _
from core.models import BaseModel
from leagues.models import League

class Season(BaseModel):
    """Сезон лиги"""
    league = models.ForeignKey(
        League,
        on_delete=models.CASCADE,
        related_name="seasons",
        verbose_name=_('Лига')
    )
    year = models.CharField(_('Год'), max_length=20)
    is_active = models.BooleanField(_('Активен'), default=False)
    external_id = models.CharField(
        _('Внешний ID'),
        max_length=100,
        unique=True,
        null=True,
        blank=True
    )

    class Meta:
        verbose_name = _('Сезон')
        verbose_name_plural = _('Сезоны')
        ordering = ['-year']
        constraints = [
            models.UniqueConstraint(fields=['league', 'year'], name='unique_league_season')
        ]

    def __str__(self):
        return f"{self.league.name} {self.year}"