# teams/models.py
from django.db import models
from django.utils.translation import gettext_lazy as _
from core.models import BaseModel
from seasons.models import Season

class Team(BaseModel):
    name = models.CharField(_('Название'), max_length=255)
    logo = models.ImageField(_('Логотип'), upload_to="teams/", null=True, blank=True)
    logo_url = models.URLField(_('URL логотипа'), null=True, blank=True)
    city = models.CharField(_('Город'), max_length=120, blank=True)
    external_id = models.CharField(
        _('Внешний ID'),
        max_length=100,
        unique=True,
        null=True,
        blank=True
    )

    is_active = models.BooleanField(_('Активна'), default=True)

    class Meta:
        verbose_name = _('Команда')
        verbose_name_plural = _('Команды')
        ordering = ['name']

    def __str__(self):
        return self.name


class TeamSeason(BaseModel):
    """Привязка команды к сезону"""
    team = models.ForeignKey(
        Team,
        on_delete=models.CASCADE,
        verbose_name=_('Команда')
    )
    season = models.ForeignKey(
        Season,
        on_delete=models.CASCADE,
        verbose_name=_('Сезон')
    )

    class Meta:
        verbose_name = _('Команда в сезоне')
        verbose_name_plural = _('Команды в сезонах')
        constraints = [
            models.UniqueConstraint(fields=['team', 'season'], name='unique_team_season')
        ]

    def __str__(self):
        return f"{self.team} — {self.season}"