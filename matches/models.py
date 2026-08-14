# matches/models.py
from django.db import models
from django.utils.translation import gettext_lazy as _
from core.models import BaseModel
from leagues.models import League
from seasons.models import Season
from teams.models import Team
from coaches.models import Coach
from referees.models import Referee
from core.models_stadium import Stadium


class Match(BaseModel):
    """Футбольный матч"""
    STATUS_CHOICES = [
        ("scheduled", _("Запланирован")),
        ("live", _("Идёт")),
        ("finished", _("Завершён")),
    ]
    
    league = models.ForeignKey(League, on_delete=models.CASCADE, verbose_name=_('Лига'))
    season = models.ForeignKey(Season, on_delete=models.CASCADE, verbose_name=_('Сезон'))
    home_team = models.ForeignKey(
        Team,
        on_delete=models.CASCADE,
        related_name="home_matches",
        verbose_name=_('Домашняя команда')
    )
    away_team = models.ForeignKey(
        Team,
        on_delete=models.CASCADE,
        related_name="away_matches",
        verbose_name=_('Гостевая команда')
    )
    home_coach = models.ForeignKey(
        Coach,
        on_delete=models.SET_NULL,
        null=True,
        related_name="home_coached_matches",
        verbose_name=_('Домашний тренер')
    )
    away_coach = models.ForeignKey(
        Coach,
        on_delete=models.SET_NULL,
        null=True,
        related_name="away_coached_matches",
        verbose_name=_('Гостевой тренер')
    )
    referee = models.ForeignKey(
        Referee,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name=_('Судья')
    )
    start_time = models.DateTimeField(_('Время начала'))
    end_time = models.DateTimeField(_('Время окончания'), null=True, blank=True)
    status = models.CharField(
        _('Статус'),
        max_length=20,
        choices=STATUS_CHOICES,
        default="scheduled"
    )
    home_score = models.IntegerField(_('Счёт дома'), null=True, blank=True, default=0)
    away_score = models.IntegerField(_('Счёт гостей'), null=True, blank=True, default=0)
    has_lineup = models.BooleanField(_('Есть состав'), default=False)
    voting_open_until = models.DateTimeField(_('Голосование до'))
    external_id = models.CharField(
        _('Внешний ID'),
        max_length=100,
        unique=True,
        null=True,
        blank=True
    )
    stadium = models.ForeignKey(
        Stadium,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name=_('Стадион')
    )
    
    class Meta:
        verbose_name = _('Матч')
        verbose_name_plural = _('Матчи')
        ordering = ['-start_time']
        indexes = [
            models.Index(fields=['status', 'start_time']),
            models.Index(fields=['status', 'end_time']), 
            models.Index(fields=['league', 'season', 'start_time']),
        ]
    
    def __str__(self):
        return f"{self.home_team} vs {self.away_team}"
    
    def get_score_display(self):
        """Корректное отображение счёта"""
        home = self.home_score if self.home_score is not None else '-'
        away = self.away_score if self.away_score is not None else '-'
        return f"{home} : {away}"

    @property
    def is_derby(self) -> bool:
        """
        НОВОЕ: матч между "принципиальными соперниками" (`Team.rivals`,
        см. teams/models.py и teams/admin.py::TeamAdmin.filter_horizontal).
        Раньше пары соперников можно было проставить только в админке, но
        нигде на сайте это никак не отображалось — бейдж "Дерби-эксперт"
        (users/badges.py) начислялся полностью незаметно для пользователя,
        который не мог понять, какие матчи вообще считаются "дерби".
        Используется в шаблонах списка/детали матча. Дёшево при
        prefetch_related('home_team__rivals') в queryset вьюхи — иначе один
        доп. запрос на матч (rivals редко больше 1-2 записей на команду).
        """
        if not self.home_team_id or not self.away_team_id:
            return False
        return any(r.id == self.away_team_id for r in self.home_team.rivals.all())

    def is_voting_open(self):
        """Проверка открыто ли голосование"""
        from django.utils import timezone
        return self.status == 'finished' and timezone.now() <= self.voting_open_until