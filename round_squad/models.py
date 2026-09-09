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
тура от источника — KFF исторически, Sportmonks с 2026-09-08 — не
меняется при переносе, см. matches/models.py::Match.tour).
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

    # "Изменение позиции" (docs/adr/0032-squad-explainability-v2.md) — те же
    # 4 значения, что у season_squad.SeasonBestXISlot.RANK_CHANGE_CHOICES,
    # НАМЕРЕННО не импортируются оттуда (round_squad и season_squad не
    # должны зависеть друг от друга ради одной константы — тот же принцип,
    # что у NOTABLE_EVENT_TYPES в round_squad/services.py). Сравнение здесь
    # идёт тур-к-туру (см. RoundPositionRanking ниже), а не батч-к-батчу
    # внутри одного тура, как в season_squad — тур пересчитывается много раз
    # ДО финализации, но "предыдущий" для rank_change — это прошлый
    # ЗАФИКСИРОВАННЫЙ тур, а не предыдущий прогон recompute этого же тура.
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
    """Полный ранжированный снимок кандидатов на слот ОДНОГО тура — тот же
    смысл, что season_squad.SeasonPositionRanking, но партия сравнения
    здесь не "предыдущий прогон recompute", а "предыдущий тур" (см.
    докстринг RoundBestXISlot.RANK_CHANGE_CHOICES выше): round_squad/services.py
    пересчитывает один и тот же тур много раз до финализации, поэтому
    сравнивать с "прошлым прогоном ЭТОГО ЖЕ тура" бесполезно — почти всегда
    SAME. recompute_round полностью перезаписывает строки этого тура на
    каждый вызов (delete + bulk_create), в отличие от season_squad, который
    хранит несколько последних батчей — здесь на слот/тур нужен только один
    актуальный снимок, "предыдущая партия" всегда однозначно тур-1."""

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
