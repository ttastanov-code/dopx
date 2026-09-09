# referees/models.py
from django.db import models
from django.utils.translation import gettext_lazy as _
from core.models import BaseModel

class Referee(BaseModel):
    """Футбольный судья"""
    first_name = models.CharField(_('Имя'), max_length=120)
    last_name = models.CharField(_('Фамилия'), max_length=120)
    country = models.CharField(_('Страна'), max_length=120, blank=True)
    # См. тот же комментарий в coaches/models.py::Coach.photo — у KFF нет
    # публичных фото судей, поле для ручной загрузки стаффом.
    photo = models.ImageField(_('Фото'), upload_to="referees/", null=True, blank=True)
    is_active = models.BooleanField(_('Активен'), default=True)
    external_id = models.CharField(
        _('Внешний ID'),
        max_length=100,
        unique=True,
        null=True,
        blank=True
    )
    # См. комментарий у League.sportmonks_id (leagues/models.py). В отличие
    # от KFF (судья приходил свободным текстом без id, см.
    # parsers/kff/importers.py::get_or_create_referee_by_name), у Sportmonks
    # судья — стабильная сущность со своим id с самого начала. Существующие
    # записи, заведённые ещё через парсинг текста KFF, сверяются со
    # Sportmonks один раз скриптом реконсиляции (фаза 2 плана), дальше
    # матчинг всегда идёт по этому полю, а не по имени.
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