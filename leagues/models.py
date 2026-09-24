# leagues/models.py
from django.db import models
from django.utils.translation import gettext_lazy as _
from core.models import BaseModel

class League(BaseModel):
    """Футбольная лига."""
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
    # ID в Sportmonks (external_id — старые KFF-данные).
    sportmonks_id = models.CharField(
        _('Sportmonks ID'),
        max_length=100,
        unique=True,
        null=True,
        blank=True
    )
    # Главная лига сайта (таблица на главной, сезон по умолчанию).
    is_primary = models.BooleanField(
        _('Главная лига сайта'),
        default=False,
        help_text=_('Турнирная таблица какой лиги показывается на главной странице. Должна быть ровно одна.'),
    )

    class Meta:
        verbose_name = _('Лига')
        verbose_name_plural = _('Лиги')
        ordering = ['name']

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        """Главная лига только одна — у остальных флаг снимается."""
        super().save(*args, **kwargs)
        if self.is_primary:
            League.objects.filter(is_primary=True).exclude(pk=self.pk).update(is_primary=False)