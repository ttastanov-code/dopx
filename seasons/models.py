# seasons/models.py
from django.db import models
from django.utils.translation import gettext_lazy as _
from core.models import BaseModel
from leagues.models import League

class Season(BaseModel):
    """Сезон лиги"""
    league = models.ForeignKey(
        League,
        on_delete=models.CASCADE,
        related_name="seasons",
        verbose_name=_('Лига')
    )
    year = models.CharField(_('Год'), max_length=20)
    is_active = models.BooleanField(_('Активен'), default=False)
    external_id = models.CharField(
        _('Внешний ID'),
        max_length=100,
        unique=True,
        null=True,
        blank=True
    )

    class Meta:
        verbose_name = _('Сезон')
        verbose_name_plural = _('Сезоны')
        ordering = ['-year']
        constraints = [
            models.UniqueConstraint(fields=['league', 'year'], name='unique_league_season')
        ]

    def __str__(self):
        return f"{self.league.name} {self.year}"

    def save(self, *args, **kwargs):
        """
        ИСПРАВЛЕНО (автоматический переход на новый сезон): `is_active` было
        обычным BooleanField без какой-либо гарантии, что у лиги активен
        РОВНО ОДИН сезон. Парсер (`parsers/kff/importers.py::
        get_or_create_season`, `AUTO_CREATE_SEASONS=True` в settings) уже
        создаёт новый Season с `is_active=True`, как только КФФ присылает
        матч нового сезона — но ничего не выключало `is_active` у
        предыдущего. В моменте это "прокатывало" только потому, что везде,
        где брался активный сезон через `.filter(is_active=True).first()`,
        `Meta.ordering = ['-year']` СЛУЧАЙНО подсовывал более свежий год
        первым — но это было хрупкое совпадение, не гарантия: в сайдбаре
        страницы лиги (templates/leagues/detail.html) при двух одновременно
        "активных" сезонах пользователь увидел бы ДВА бейджа "Активен"
        сразу, а `aggregates/tasks.py::recalculate_season_standings`
        (Celery Beat, каждые 10 минут, без явного season_id) полагается
        именно на то, что активный сезон — один.

        Теперь при сохранении сезона с `is_active=True` все ОСТАЛЬНЫЕ
        сезоны этой же лиги атомарно снимаются с активности — это
        гарантирует ровно один активный сезон на лигу при ЛЮБОМ пути
        создания/изменения (парсер, админка, скрипт), а не только при
        сохранении через конкретную вьюху. Именно это делает переход на
        новый сезон полностью автоматическим: как только парсер встретит
        первый матч следующего сезона и создаст для него Season с
        is_active=True, текущий сезон сам перестанет считаться активным —
        ручное вмешательство не требуется.
        """
        super().save(*args, **kwargs)
        if self.is_active:
            Season.objects.filter(
                league_id=self.league_id, is_active=True
            ).exclude(pk=self.pk).update(is_active=False)