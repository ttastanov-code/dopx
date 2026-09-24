# referees/models.py
from django.db import models
from django.utils.translation import gettext_lazy as _
from core.models import BaseModel, NAME_SOURCE_CHOICES

class Referee(BaseModel):
    """Футбольный судья."""
    first_name = models.CharField(_('Имя'), max_length=120)
    last_name = models.CharField(_('Фамилия'), max_length=120)
    # Источник ФИО.
    name_source = models.CharField(
        _('Источник ФИО'), max_length=30, choices=NAME_SOURCE_CHOICES, blank=True, db_index=True,
    )
    country = models.CharField(_('Страна'), max_length=120, blank=True)
    # Фото — только ручная загрузка.
    photo = models.ImageField(_('Фото'), upload_to="referees/", null=True, blank=True)
    is_active = models.BooleanField(_('Активен'), default=True)
    external_id = models.CharField(
        _('Внешний ID'),
        max_length=100,
        unique=True,
        null=True,
        blank=True
    )
    # ID в Sportmonks — матчинг по нему, не по имени.
    sportmonks_id = models.CharField(
        _('Sportmonks ID'),
        max_length=100,
        unique=True,
        null=True,
        blank=True
    )

    class Meta:
        verbose_name = _('Судья')
        verbose_name_plural = _('Судьи')
        ordering = ['last_name', 'first_name']

    def __str__(self):
        return f"{self.first_name} {self.last_name}"

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}"