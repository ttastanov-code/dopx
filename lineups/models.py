# lineups/models.py
from django.db import models
from django.utils.translation import gettext_lazy as _
from core.models import BaseModel
from matches.models import Match
from teams.models import Team
from players.models import Player

class MatchLineup(BaseModel):
    """Состав команды на матч"""
    SIDES = [
        ("home", _("Домашние")),
        ("away", _("Гостевые")),
    ]

    match = models.ForeignKey(
        Match,
        on_delete=models.CASCADE,
        related_name="lineups",
        verbose_name=_('Матч')
    )
    team = models.ForeignKey(
        Team,
        on_delete=models.CASCADE,
        verbose_name=_('Команда')
    )
    side = models.CharField(
        _('Сторона'),
        max_length=10,
        choices=SIDES
    )
    formation = models.CharField(
        _('Формация'),
        max_length=20,
        blank=True
    )

    class Meta:
        verbose_name = _('Состав на матч')
        verbose_name_plural = _('Составы на матчи')
        constraints = [
            models.UniqueConstraint(fields=['match', 'team'], name='unique_match_team_lineup')
        ]

    def __str__(self):
        return f"{self.match} — {self.team} ({self.side})"


class MatchLineupPlayer(BaseModel):
    """Игрок в составе на матч"""
    lineup = models.ForeignKey(
        MatchLineup,
        on_delete=models.CASCADE,
        related_name="players",
        verbose_name=_('Состав')
    )
    player = models.ForeignKey(
        Player,
        on_delete=models.CASCADE,
        verbose_name=_('Игрок')
    )
    is_starting = models.BooleanField(_('В старте'), default=True)
    position = models.CharField(
        _('Позиция'),
        max_length=20,
        blank=True
    )
    field_position = models.CharField(
        _('Сторона на поле'),
        max_length=5,
        blank=True,
        help_text=_(
            "Сырое значение 'field_position' из ответа KFF "
            "(/games/<id>/lineup) — C/L/R/LC/RC, колонка формации "
            "(не путать с амплуа в поле 'Позиция' выше). Найдено "
            "2026-08-23: это поле присутствовало в ответе API у КАЖДОГО "
            "игрока состава всё это время, но не импортировалось — "
            "именно оно даёт сторону поля (левый/правый/центр), которой "
            "не хватало для точного распределения слотов формации в "
            "season_squad/round_squad (см. players/positions.py)."
        )
    )
    shirt_number = models.IntegerField(
        _('Номер'),
        null=True,
        blank=True
    )
    minute_in = models.IntegerField(
        _('Минута выхода'),
        null=True,
        blank=True
    )
    minute_out = models.IntegerField(
        _('Минута замены'),
        null=True,
        blank=True
    )

    class Meta:
        verbose_name = _('Игрок в составе')
        verbose_name_plural = _('Игроки в составе')
        ordering = ['is_starting', 'shirt_number']
        indexes = [
            models.Index(fields=['lineup', 'is_starting']),
            models.Index(fields=['player', 'lineup']),
        ]

    def __str__(self):
        return f"{self.player} ({self.shirt_number or '?'})"