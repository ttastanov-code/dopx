# season_squad/models.py
"""
"Живая сборная сезона" — состав 4-3-3, который каждые N минут пересчитывается
по свежим оценкам пользователей (см. season_squad/services.py::recompute_best_xi
и season_squad/tasks.py — Celery Beat).

Схема из трёх моделей:

- SeasonBestXI — один "контейнер" на сезон (OneToOne). Хранит формацию и
  флаг is_final: пока сезон идёт, это "живой", постоянно перезаписываемый
  состав; когда сезон закрыт и голосования завершены, стафф жмёт
  "Зафиксировать" в админке — is_final=True, и recompute больше не трогает
  эту запись (см. admin.py::mark_as_final).

- SeasonBestXISlot — 11 карточек стартового состава + 2 доп. карточки
  (тренер, судья) = 13 строк на живую сборную. Это ДЕНОРМАЛИЗОВАННЫЙ снимок
  "кто сейчас лучший на этой позиции" — специально дублирует имя/фото/клуб
  строками, чтобы страницу можно было отрендерить БЕЗ единого join'а и
  безопасно поллить через HTMX каждые несколько минут, не нагружая БД.
  occupant (GenericForeignKey) — Player, Coach или Referee, в зависимости
  от slot_code: обычные позиции → Player, 'COACH' → Coach, 'REFEREE' →
  Referee. GenericForeignKey — потому что это три разных модели с общим
  смыслом "лучший в своей роли", а не потому что where-то нужен полиморфизм
  ради полиморфизма.

- SeasonPositionRanking — полный ранжированный список кандидатов на слот на
  момент каждого пересчёта (не только победитель). Нужен для двух вещей:
  1) посчитать rank_change (сравнить ранг текущего occupant'а в ЭТОМ
     пересчёте с его рангом в ПРЕДЫДУЩЕМ пересчёте для того же слота);
  2) задел на будущее — полноценный лидерборд "топ-5 претендентов на
     позицию" без пересчёта на лету.
  Хранит несколько последних "партий" (batch = один computed_at на все
  строки одного recompute), старые батчи чистит сама таска (см. tasks.py).

ВАЖНО про rank_change: occupant слота — по определению ранг №1 в своём пуле
кандидатов НА ЭТОТ МОМЕНТ. Значит относительно предыдущего пересчёта он
может быть только "уже был №1" (SAME), "поднялся с ранга N" (UP, delta=N-1)
или "не участвовал в прошлом пересчёте" (NEW, не хватало данных или не
было в лиге). Формально текущий occupant не может быть "хуже, чем раньше"
— иначе он бы не был occupant'ом сейчас. Поэтому DOWN в практике этой
страницы никогда не появится на карточках стартового состава; поле и choice
оставлены в модели заранее — пригодятся, если позже добавим блок "кто
вылетел из состава" (там DOWN как раз естественнен: был occupant'ом, стал
вторым).
"""
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.utils.translation import gettext_lazy as _

from core.models import BaseModel


class SeasonBestXI(BaseModel):
    """Контейнер живой сборной одного сезона."""

    season = models.OneToOneField(
        'seasons.Season',
        on_delete=models.CASCADE,
        related_name='best_xi',
        verbose_name=_('Сезон'),
    )
    formation = models.CharField(_('Формация'), max_length=20, default='4-3-3')
    is_final = models.BooleanField(
        _('Зафиксирована как итоговая'),
        default=False,
        help_text=_(
            'После включения recompute больше не трогает эту сборную — '
            'используется после закрытия сезона и последних голосований.'
        ),
    )
    finalized_at = models.DateTimeField(_('Зафиксирована'), null=True, blank=True)
    last_computed_at = models.DateTimeField(_('Последний пересчёт'), null=True, blank=True)

    class Meta:
        verbose_name = _('Сборная DOPX')
        verbose_name_plural = _('Сборные DOPX')

    def __str__(self):
        state = 'итоговая' if self.is_final else 'живая'
        return f"Сборная DOPX сезона {self.season} ({state})"


class SeasonBestXISlot(BaseModel):
    """Одна карточка на странице: лучший на позиции/тренер/судья."""

    RANK_CHANGE_NEW = 'new'
    RANK_CHANGE_UP = 'up'
    RANK_CHANGE_DOWN = 'down'
    RANK_CHANGE_SAME = 'same'
    RANK_CHANGE_CHOICES = [
        (RANK_CHANGE_NEW, _('Вошёл в состав')),
        (RANK_CHANGE_UP, _('Поднялся')),
        (RANK_CHANGE_DOWN, _('Опустился')),
        (RANK_CHANGE_SAME, _('Без изменений')),
    ]

    best_xi = models.ForeignKey(
        SeasonBestXI, on_delete=models.CASCADE, related_name='slots', verbose_name=_('Сборная'),
    )
    slot_code = models.CharField(_('Код слота'), max_length=10)
    order = models.PositiveSmallIntegerField(_('Порядок отображения'), default=0)

    # Occupant — Player / Coach / Referee, в зависимости от slot_code.
    content_type = models.ForeignKey(
        ContentType, on_delete=models.CASCADE, null=True, blank=True,
    )
    object_id = models.UUIDField(null=True, blank=True)
    occupant = GenericForeignKey('content_type', 'object_id')

    # Денормализация для рендера без join'ов (см. докстринг модуля).
    occupant_name = models.CharField(_('Имя'), max_length=255, blank=True)
    occupant_team_name = models.CharField(_('Клуб'), max_length=255, blank=True)
    occupant_photo_url = models.CharField(_('URL фото'), max_length=500, blank=True)
    occupant_profile_url = models.CharField(_('Ссылка на профиль'), max_length=500, blank=True)

    season_score = models.FloatField(_('Рейтинг сезона'), null=True, blank=True)
    matches_count = models.PositiveIntegerField(_('Матчей'), default=0)
    votes_count = models.PositiveIntegerField(_('Голосов'), default=0)
    is_confident = models.BooleanField(_('Достаточно данных'), default=False)

    rank_change = models.CharField(
        _('Изменение'), max_length=10, choices=RANK_CHANGE_CHOICES, default=RANK_CHANGE_NEW,
    )
    rank_change_delta = models.PositiveSmallIntegerField(_('На сколько мест'), null=True, blank=True)

    explanation = models.TextField(_('Почему в XI'), blank=True)

    class Meta:
        verbose_name = _('Слот сборной')
        verbose_name_plural = _('Слоты сборной')
        ordering = ['order']
        constraints = [
            models.UniqueConstraint(fields=['best_xi', 'slot_code'], name='unique_best_xi_slot'),
        ]
        indexes = [
            models.Index(fields=['content_type', 'object_id']),
        ]

    def __str__(self):
        return f"{self.slot_code}: {self.occupant_name or '—'}"


class SeasonPositionRanking(BaseModel):
    """Полный ранжированный снимок кандидатов на слот на момент пересчёта."""

    best_xi = models.ForeignKey(
        SeasonBestXI, on_delete=models.CASCADE, related_name='rankings', verbose_name=_('Сборная'),
    )
    slot_code = models.CharField(_('Код слота'), max_length=10)

    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.UUIDField()
    occupant = GenericForeignKey('content_type', 'object_id')

    rank = models.PositiveSmallIntegerField(_('Ранг в пуле'))
    season_score = models.FloatField(_('Рейтинг сезона'))
    matches_count = models.PositiveIntegerField(_('Матчей'), default=0)
    votes_count = models.PositiveIntegerField(_('Голосов'), default=0)

    # Общий для всех строк одного recompute-прогона — по нему находим
    # "предыдущую партию" для сравнения рангов (см. services.py).
    computed_at = models.DateTimeField(_('Партия пересчёта'))

    class Meta:
        verbose_name = _('Ранг кандидата в сборную')
        verbose_name_plural = _('Ранги кандидатов в сборную')
        ordering = ['slot_code', 'rank']
        indexes = [
            models.Index(fields=['best_xi', 'slot_code', 'computed_at']),
            models.Index(fields=['best_xi', 'slot_code', 'rank']),
            models.Index(fields=['content_type', 'object_id']),
        ]

    def __str__(self):
        return f"{self.slot_code} #{self.rank} @ {self.computed_at:%Y-%m-%d %H:%M}"
