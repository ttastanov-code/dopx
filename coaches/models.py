# coaches/models.py
from django.db import models
from django.utils.translation import gettext_lazy as _
from core.models import BaseModel
from teams.models import Team

class Coach(BaseModel):
    """Футбольный тренер"""
    first_name = models.CharField(_('Имя'), max_length=120)
    last_name = models.CharField(_('Фамилия'), max_length=120)
    team = models.ForeignKey(
        Team,
        on_delete=models.SET_NULL,
        null=True,
        related_name="coaches",
        verbose_name=_('Команда')
    )
    # KFF фото есть только у игроков (см. season_squad/photo_scraper.py) —
    # у тренеров нет публичного источника фото, поле для ручной загрузки
    # стаффом через админку (нужно карточке "Живой сборной сезона").
    photo = models.ImageField(_('Фото'), upload_to="coaches/", null=True, blank=True)
    is_active = models.BooleanField(_('Активен'), default=True)
    external_id = models.CharField(
        _('Внешний ID'),
        max_length=100,
        unique=True,
        null=True,
        blank=True
    )

    class Meta:
        verbose_name = _('Тренер')
        verbose_name_plural = _('Тренеры')
        ordering = ['last_name', 'first_name']

    def __str__(self):
        return f"{self.first_name} {self.last_name}"

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}"