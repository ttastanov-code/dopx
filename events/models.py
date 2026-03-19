# events/models.py
from django.db import models
from django.utils.translation import gettext_lazy as _
from core.models import BaseModel
from matches.models import Match
from players.models import Player

class MatchEvent(BaseModel):
    """Событие матча (гол, карточка, замена)"""
    EVENT_TYPES = [
        ("goal", _("Гол")),
        ("yellow_card", _("Жёлтая карточка")),
        ("red_card", _("Красная карточка")),
        ("substitution", _("Замена")),
        ("penalty", _("Пенальти")),
        ("own_goal", _("Автогол")),
    ]

    TEAM_SIDES = [
        ("home", _("Домашние")),
        ("away", _("Гостевые")),
    ]

    match = models.ForeignKey(
        Match,
        on_delete=models.CASCADE,
        related_name="events",
        verbose_name=_('Матч')
    )
    player = models.ForeignKey(
        Player,
        on_delete=models.SET_NULL,
        null=True,
        verbose_name=_('Игрок')
    )
    minute = models.IntegerField(_('Минута'))
    event_type = models.CharField(
        _('Тип события'),
        max_length=30,
        choices=EVENT_TYPES
    )
    team_side = models.CharField(
        _('Сторона'),
        max_length=10,
        choices=TEAM_SIDES
    )
    external_id = models.CharField(
        _('Внешний ID'),
        max_length=100,
        unique=True
    )

    class Meta:
        verbose_name = _('Событие матча')
        verbose_name_plural = _('События матчей')
        ordering = ['minute']
        indexes = [
            models.Index(fields=['match', 'minute']),
            models.Index(fields=['player', 'event_type']),
        ]

    def __str__(self):
        return f"{self.get_event_type_display()} — {self.minute}'"