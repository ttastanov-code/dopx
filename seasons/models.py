# seasons/models.py
from django.db import models
from django.utils.translation import gettext_lazy as _
from core.models import BaseModel
from leagues.models import League

class Season(BaseModel):
    """Сезон лиги."""
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
    # ID в Sportmonks.
    sportmonks_id = models.CharField(
        _('Sportmonks ID'),
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

    def save(self, *args, **kwargs):
        """Активный сезон на лигу только один — у остальных флаг снимается."""
        super().save(*args, **kwargs)
        if self.is_active:
            Season.objects.filter(
                league_id=self.league_id, is_active=True
            ).exclude(pk=self.pk).update(is_active=False)

    @classmethod
    def get_primary_active(cls):
        """Сезон по умолчанию — активный сезон главной лиги.
        Fallback, если главная лига не отмечена.
        """
        season = cls.objects.filter(is_active=True, league__is_primary=True).select_related('league').first()
        if season:
            return season
        return cls.objects.filter(is_active=True).select_related('league').first()

    def previous(self):
        """Предыдущий сезон той же лиги."""
        return (
            Season.objects.filter(league_id=self.league_id, year__lt=self.year)
            .select_related('league').order_by('-year').first()
        )

    @classmethod
    def get_display_season(cls, has_data=None):
        """Сезон для итоговых блоков: активный, а пока в нём нет данных — предыдущий.
        has_data(season) -> bool; по умолчанию — есть завершённые матчи.
        """
        season = cls.get_primary_active()
        if season is None:
            return None
        if has_data is None:
            from matches.models import Match

            def has_data(s):
                return Match.objects.filter(season=s, status='finished').exists()
        if has_data(season):
            return season
        prev = season.previous()
        return prev if prev is not None and has_data(prev) else season

    @classmethod
    def get_roster_season(cls):
        """Сезон для списков команд/игроков: активный, пока без состава лиги — предыдущий."""
        from teams.models import TeamSeason

        return cls.get_display_season(has_data=lambda s: TeamSeason.objects.filter(season=s).exists())
