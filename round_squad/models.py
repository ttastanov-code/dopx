# round_squad/models.py
"""
"DOPX Лучшие тура" (бренд-название; до правки 2026-08-22 называлось "Тур
недели" — переименовано намеренно, см. ниже про переносы) — снимок лучшего
состава ОДНОГО тура чемпионата. Продуктовый запрос 2026-08-22 (по мотивам
ревью ChatGPT Codex по season_squad): "Сборная тура"/"Игрок тура" — тот же
принцип, что Sofascore Team of the Week, поверх уже накопленной
инфраструктуры season_squad (переиспользуем players/positions.py::
SLOT_PROCESSING_ORDER и паттерн денормализации карточек через
GenericForeignKey — см. докстринг season_squad/models.py).

ПОЧЕМУ НЕ "Тур НЕДЕЛИ": на одной календарной неделе из-за переносов матчей
могут играться матчи РАЗНЫХ туров одновременно (перенесённый матч 5-го тура
может сыграться в календарную неделю 9-го) — название "тур недели"
подразумевает привязку к календарю, которой в модели данных нет и не
должно быть: единственный устойчивый идентификатор — Match.tour (номер
тура от KFF, не меняется при переносе, см. matches/models.py::Match.tour).
RoundBestXI ключуется строго по (season, tour), никогда по диапазону дат —
поэтому переименование в "DOPX Лучшие N тура" не требует правок алгоритма,
только копирайта: механика и раньше была тур-центричной, только название
вводило в заблуждение.

КЛЮЧЕВОЕ ОТЛИЧИЕ ОТ season_squad: там кандидат копит рейтинг за МНОГО
матчей сезона, и число матчей — прямой сигнал надёжности (байесовское
сглаживание по SHRINKAGE_C "виртуальных матчей"). В туре у игрока почти
всегда РОВНО один оценённый матч — число матчей тут бесполезно как сигнал.
Сигнал надёжности здесь — число ГОЛОСОВ за этот единственный матч
(зрелищное дерби соберёт 40+ голосов, рядовой матч в будний день — 5).
Поэтому round_squad/services.py сглаживает по голосам (ROUND_VOTE_SHRINKAGE_C),
а не по матчам — это осознанно другая ось, не переиспользуем season_squad.SHRINKAGE_C.

ЖИЗНЕННЫЙ ЦИКЛ RoundBestXI.is_final — тоже отличается от season_squad, где
это ручное действие стаффа после конца сезона. Тур закрывается САМ: как
только у ВСЕХ матчей этого тура voting_open_until в прошлом, донакрутить
состав больше нечем (новых голосов по сыгранным матчам тура уже не будет),
и recompute_round() в round_squad/services.py взводит is_final=True
автоматически при следующем прогоне. До этого момента recompute можно
вызывать сколько угодно раз (Celery Beat, см. round_squad/tasks.py) — тур
"живой", как и live-сборная сезона.
"""
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.utils.translation import gettext_lazy as _

from core.models import BaseModel


class RoundBestXI(BaseModel):
    """Контейнер «DOPX Лучшие тура» — один на пару (сезон, номер тура)."""

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

    # --- «Игрок тура» — лучший ОБЩИЙ результат тура, независимо от позиции
    # и слота в формации (может как совпадать, так и не совпадать с
    # occupant'ом соответствующего слота в RoundBestXISlot — см. докстринг
    # round_squad/services.py::_rank_round_pool). Денормализовано по тому
    # же принципу, что и RoundBestXISlot ниже — без join'ов для рендера.
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

    # --- Самый драматичный матч тура — по MatchEvaluation.entertainment *
    # MatchEvaluation.tension, усреднённому по матчу (см. services.py).
    most_dramatic_match = models.ForeignKey(
        'matches.Match', on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
        verbose_name=_('Самый драматичный матч'),
    )
    most_dramatic_match_score = models.FloatField(_('Индекс драмы'), null=True, blank=True)
    most_dramatic_match_explanation = models.TextField(_('Почему этот матч'), blank=True)

    # Путь в MEDIA к готовой PNG-карточке для шеринга (core/services/share_cards.py
    # ::build_round_squad_share_card) — генерируется один раз при взведении
    # is_final, тот же ленивый принцип "по первому запросу", что у остальных
    # share-карточек (см. докстринг share_cards.py), только триггер здесь —
    # не HTTP-запрос, а сам момент финализации тура в recompute_round().
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
        """Единая точка правды для названия — используется на странице,
        в embed-виджете, share-карточке, письме и админке, чтобы бренд
        не разъехался по копипастам (см. докстринг модуля про
        переименование из "Тур недели")."""
        return f"DOPX Лучшие {self.tour}-го тура"


class RoundBestXISlot(BaseModel):
    """Одна карточка состава тура: 11 полевых позиций + тренер тура (без
    судьи — Codex-ревью и продуктовый запрос про «DOPX Лучшие тура»
    ограничили первую версию игроками/тренером/самым драматичным матчем)."""

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
