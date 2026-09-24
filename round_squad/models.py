# round_squad/models.py
"""«DOPX Лучшие тура» — лучший состав одного тура.
Ключ — (season, tour), не даты: перенесённые матчи остаются в своём туре.
Сглаживание — по числу голосов за матч (ROUND_VOTE_SHRINKAGE_C).
Тур финализируется сам (is_final), когда у всех его матчей закрыто голосование.
"""
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.utils.translation import gettext_lazy as _

from core.models import BaseModel


class RoundBestXI(BaseModel):
    """«DOPX Лучшие тура» — один на (сезон, тур)."""

    season = models.ForeignKey(
        'seasons.Season',
        on_delete=models.CASCADE,
        related_name='round_squads',
        verbose_name=_('Сезон'),
    )
    tour = models.PositiveSmallIntegerField(_('Тур'))
    formation = models.CharField(_('Формация'), max_length=20, default='4-3-3')

    is_final = models.BooleanField(
        _('Зафиксирован'),
        default=False,
        help_text=_(
            'Взводится автоматически, когда голосование по всем матчам тура '
            'закрыто (см. докстринг модели) — не требует ручного действия стаффа.'
        ),
    )
    finalized_at = models.DateTimeField(_('Зафиксирован'), null=True, blank=True)
    last_computed_at = models.DateTimeField(_('Последний пересчёт'), null=True, blank=True)

    # --- «Игрок тура» — лучший результат тура вне зависимости от позиции ---
    player_of_round_content_type = models.ForeignKey(
        ContentType, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    player_of_round_object_id = models.UUIDField(null=True, blank=True)
    player_of_round = GenericForeignKey('player_of_round_content_type', 'player_of_round_object_id')
    player_of_round_name = models.CharField(_('Игрок тура'), max_length=255, blank=True)
    player_of_round_team_name = models.CharField(_('Клуб'), max_length=255, blank=True)
    player_of_round_photo_url = models.CharField(_('URL фото'), max_length=500, blank=True)
    player_of_round_profile_url = models.CharField(_('Ссылка на профиль'), max_length=500, blank=True)
    player_of_round_score = models.FloatField(_('Рейтинг тура'), null=True, blank=True)
    player_of_round_votes = models.PositiveIntegerField(_('Голосов'), default=0)
    player_of_round_explanation = models.TextField(_('Почему игрок тура'), blank=True)

    # --- Самый драматичный матч тура (entertainment * tension) ---
    most_dramatic_match = models.ForeignKey(
        'matches.Match', on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
        verbose_name=_('Самый драматичный матч'),
    )
    most_dramatic_match_score = models.FloatField(_('Индекс драмы'), null=True, blank=True)
    most_dramatic_match_explanation = models.TextField(_('Почему этот матч'), blank=True)

    # PNG для шеринга, генерируется при финализации тура.
    share_card_path = models.CharField(_('Путь к share-карточке'), max_length=255, blank=True)

    class Meta:
        verbose_name = _('DOPX Лучшие тура')
        verbose_name_plural = _('DOPX Лучшие тура')
        ordering = ['-season__year', '-tour']
        constraints = [
            models.UniqueConstraint(fields=['season', 'tour'], name='unique_round_best_xi'),
        ]

    def __str__(self):
        state = 'зафиксирован' if self.is_final else 'живой'
        return f"{self.brand_title} ({state})"

    @property
    def brand_title(self) -> str:
        """Название для страницы, виджета, карточки, письма и админки."""
        return f"DOPX Лучшие {self.tour}-го тура"


class RoundBestXISlot(BaseModel):
    """Карточка слота: 11 позиций + тренер тура."""

    # Изменение позиции относительно прошлого зафиксированного тура.
    RANK_CHANGE_NEW = 'new'
    RANK_CHANGE_UP = 'up'
    RANK_CHANGE_DOWN = 'down'
    RANK_CHANGE_SAME = 'same'
    RANK_CHANGE_CHOICES = [
        (RANK_CHANGE_NEW, _('Не играл в прошлом туре')),
        (RANK_CHANGE_UP, _('Поднялся')),
        (RANK_CHANGE_DOWN, _('Опустился')),
        (RANK_CHANGE_SAME, _('Без изменений')),
    ]

    round_best_xi = models.ForeignKey(
        RoundBestXI, on_delete=models.CASCADE, related_name='slots', verbose_name=_('Тур'),
    )
    slot_code = models.CharField(_('Код слота'), max_length=10)
    order = models.PositiveSmallIntegerField(_('Порядок отображения'), default=0)

    content_type = models.ForeignKey(
        ContentType, on_delete=models.CASCADE, null=True, blank=True,
    )
    object_id = models.UUIDField(null=True, blank=True)
    occupant = GenericForeignKey('content_type', 'object_id')

    occupant_name = models.CharField(_('Имя'), max_length=255, blank=True)
    occupant_team_name = models.CharField(_('Клуб'), max_length=255, blank=True)
    occupant_photo_url = models.CharField(_('URL фото'), max_length=500, blank=True)
    occupant_profile_url = models.CharField(_('Ссылка на профиль'), max_length=500, blank=True)

    round_score = models.FloatField(_('Рейтинг тура'), null=True, blank=True)
    votes_count = models.PositiveIntegerField(_('Голосов'), default=0)
    is_confident = models.BooleanField(_('Достаточно данных'), default=False)

    rank_change = models.CharField(
        _('Изменение'), max_length=10, choices=RANK_CHANGE_CHOICES, default=RANK_CHANGE_NEW,
    )
    rank_change_delta = models.PositiveSmallIntegerField(_('На сколько мест'), null=True, blank=True)

    explanation = models.TextField(_('Почему в составе тура'), blank=True)

    class Meta:
        verbose_name = _('Слот тура')
        verbose_name_plural = _('Слоты тура')
        ordering = ['order']
        constraints = [
            models.UniqueConstraint(fields=['round_best_xi', 'slot_code'], name='unique_round_best_xi_slot'),
        ]
        indexes = [
            models.Index(fields=['content_type', 'object_id']),
        ]

    def __str__(self):
        return f"{self.slot_code}: {self.occupant_name or '—'}"


class RoundPositionRanking(BaseModel):
    """Ранжирование кандидатов на слот одного тура. Перезаписывается при каждом пересчёте."""

    round_best_xi = models.ForeignKey(
        RoundBestXI, on_delete=models.CASCADE, related_name='rankings', verbose_name=_('Тур'),
    )
    slot_code = models.CharField(_('Код слота'), max_length=10)

    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.UUIDField()
    occupant = GenericForeignKey('content_type', 'object_id')

    rank = models.PositiveSmallIntegerField(_('Ранг в пуле'))
    round_score = models.FloatField(_('Рейтинг тура'))
    votes_count = models.PositiveIntegerField(_('Голосов'), default=0)

    class Meta:
        verbose_name = _('Ранг кандидата в тур')
        verbose_name_plural = _('Ранги кандидатов в тур')
        ordering = ['slot_code', 'rank']
        constraints = [
            models.UniqueConstraint(
                fields=['round_best_xi', 'slot_code', 'rank'], name='unique_round_position_ranking',
            ),
        ]
        indexes = [
            models.Index(fields=['round_best_xi', 'slot_code']),
            models.Index(fields=['content_type', 'object_id']),
        ]

    def __str__(self):
        return f"{self.slot_code} #{self.rank} @ тур {self.round_best_xi.tour}"
