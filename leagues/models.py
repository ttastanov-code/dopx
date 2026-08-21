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
    # Какая лига считается "главной" для сайта — используется вместо
    # Season.objects.filter(is_active=True).first() (без фильтра по лиге)
    # в core/views.py::standings_preview и core/views.py (главная страница):
    # с одной лигой на сайте .first() случайно давал правильный ответ, но
    # как только появится вторая лига с собственным активным сезоном
    # (например, Кубок Казахстана), выбор таблицы на главной стал бы
    # зависеть от Season.Meta.ordering, а не от осмысленного решения.
    # См. docs/BACKLOG.md, находка 1.
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
        """При сохранении с is_primary=True все остальные лиги атомарно
        снимаются с этого флага — гарантирует ровно одну главную лигу
        сайта при любом пути создания (админка, миграция, скрипт), тот же
        паттерн, что и Season.is_active (seasons/models.py)."""
        super().save(*args, **kwargs)
        if self.is_primary:
            League.objects.filter(is_primary=True).exclude(pk=self.pk).update(is_primary=False)