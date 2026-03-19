# leagues/models.py
from django.db import models
from django.utils.translation import gettext_lazy as _
from core.models import BaseModel

class League(BaseModel):
    """Футбольная лига"""
    name = models.CharField(_('Название'), max_length=255)
    country = models.CharField(_('Страна'), max_length=255)
    logo = models.ImageField(_('Логотип'), upload_to='leagues/', null=True, blank=True)
    external_id = models.CharField(
        _('Внешний ID'),
        max_length=100,
        unique=True,
        null=True,
        blank=True
    )

    class Meta:
        verbose_name = _('Лига')
        verbose_name_plural = _('Лиги')
        ordering = ['name']

    def __str__(self):
        return self.name