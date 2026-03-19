# players/models.py
from django.db import models
from django.utils.translation import gettext_lazy as _
from core.models import BaseModel
from teams.models import Team

class Player(BaseModel):
    """Футбольный игрок"""
    first_name = models.CharField(_('Имя'), max_length=120)
    last_name = models.CharField(_('Фамилия'), max_length=120)
    team = models.ForeignKey(
        Team,
        on_delete=models.SET_NULL,
        null=True,
        related_name="players",
        verbose_name=_('Команда')
    )
    position = models.CharField(_('Позиция'), max_length=50, blank=True)
    number = models.IntegerField(_('Номер'), null=True, blank=True)
    photo = models.ImageField(_('Фото'), upload_to="players/", null=True, blank=True)
    is_active = models.BooleanField(_('Активен'), default=True)
    external_id = models.CharField(
        _('Внешний ID'),
        max_length=100,
        unique=True,
        null=True,
        blank=True
    )

    class Meta:
        verbose_name = _('Игрок')
        verbose_name_plural = _('Игроки')
        ordering = ['last_name', 'first_name']
        indexes = [
            models.Index(fields=['team', 'is_active']),
            models.Index(fields=['last_name', 'first_name']),
        ]

    def __str__(self):
        return f"{self.first_name} {self.last_name}"

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}"