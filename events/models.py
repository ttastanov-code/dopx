# events/models.py
from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _
from core.models import BaseModel
from matches.models import Match
from players.models import Player


class MatchEvent(BaseModel):
    """Событие матча с расширенной информацией"""
    
    EVENT_TYPES = [
        ("goal", _("Гол")),
        ("yellow_card", _("Жёлтая карточка")),
        ("red_card", _("Красная карточка")),
        ("substitution", _("Замена")),
        ("penalty", _("Пенальти")),
        ("own_goal", _("Автогол")),
        ("var_check", _("VAR проверка")),
        # ДОБАВЛЕНО 2026-09-01: parsers/kff/importers.py::_is_goal_disallowed
        # уже давно переклассифицирует "goal"/"penalty"/"own_goal" в
        # "disallowed_goal", если API отмечает гол отменённым/пересмотренным
        # VAR — но этого значения не было в EVENT_TYPES. choices у CharField
        # не enforced на уровне БД, поэтому запись НЕ падала, но
        # get_event_type_display() отдавал бы сырой код вместо человеческого
        # текста везде, где рендерится лента событий. Заодно это ровно тот
        # тип, который push-уведомления о live-событиях (notifications/
        # tasks.py::notify_followers_match_event) используют для "гол
        # отменён" — пользователь явно просил такой пуш.
        ("disallowed_goal", _("Гол отменён")),
    ]
    
    TEAM_SIDES = [
        ("home", _("Домашние")),
        ("away", _("Гостевые")),
    ]
    
    CARD_REASONS = [
        ("unsporting", "Неспортивное поведение"),
        ("dissent", "Диссидентство"),
        ("persistent_fouling", "Систематические нарушения"),
        ("delaying_restart", "Задержка возобновления"),
        ("entering_field", "Незаконный выход на поле"),
        ("other", "Другое"),
    ]
    
    # Основные поля
    match = models.ForeignKey(
        'matches.Match',
        on_delete=models.CASCADE,
        related_name='events'
    )
    minute = models.PositiveSmallIntegerField(help_text="Минута события")
    added_time = models.PositiveSmallIntegerField(
        default=0,
        help_text="Добавленное время (+1, +2...)"
    )
    event_type = models.CharField(max_length=20, choices=EVENT_TYPES)
    team_side = models.CharField(max_length=4, choices=TEAM_SIDES)
    
    # Игрок, к которому относится событие
    player = models.ForeignKey(
        'players.Player',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='events'
    )
    
    # 🔥 НОВЫЕ ПОЛЯ для детализации:
    
    # Для голов: кто отдал пас
    assist_player = models.ForeignKey(
        'players.Player',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='assists'
    )
    
    # Для голов: счёт после гола
    score_after = models.CharField(
        max_length=5,
        blank=True,
        null=True,  # ← ДОБАВИТЬ эту строку
        help_text="Например: 2-1"
    )
    
    # Для замен: игрок, который ушёл с поля
    player_out = models.ForeignKey(
        'players.Player',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='substitutions_out'
    )
    
    # Для карточек: причина (опционально)
    card_reason = models.CharField(
        max_length=30,
        choices=CARD_REASONS,
        blank=True,
        null=True
    )
    
    # Для VAR: результат проверки
    var_decision = models.CharField(
        max_length=50,
        blank=True,
        help_text="Решение после VAR"
    )
    
    # Дополнительные данные из API (JSON)
    extra_data = models.JSONField(
        default=dict,
        blank=True,
        help_text="Сырые данные из API"
    )

    # ДОБАВЛЕНО (2026-09-21, жалоба пользователя — скриншот матча Астана-
    # Кайрат: судья показал жёлтую, после просмотра VAR заменил её на
    # красную тому же игроку, а в ленте события остались ОБЕ карточки, как
    # будто это два разных нарушения). КОРНЕВАЯ ПРИЧИНА — parsers/sportmonks/
    # importers.py::import_events сопоставлял "то же самое" событие с уже
    # сохранённым ТОЛЬКО по эвристике (minute, event_type, team_side), без
    # привязки к стабильному id события от Sportmonks. Когда VAR меняет тип
    # события (жёлтая → красная) для ОДНОГО И ТОГО ЖЕ event.id — event_type
    # входит в ключ сопоставления, поэтому это распознавалось как НОВОЕ
    # событие, а старая (уже неверная) жёлтая карточка навсегда оставалась
    # висеть в ленте — import_events прямым текстом заявлял, что "не
    # удаляет пропавшие события" (осознанное решение, но оно не учитывало
    # случай "событие не пропало, а было ИСПРАВЛЕНО с тем же id"). Sportmonks
    # v3 гарантированно отдаёт стабильный числовой `id` на каждый объект
    # events[] (подтверждено docs.sportmonks.com/v3, events include) — это
    # поле хранит его, чтобы повторный импорт находил и ОБНОВЛЯЛ ту же
    # запись (включая event_type), а не плодил дубли при любой последующей
    # коррекции Sportmonks (VAR, техническая правка и т.п.).
    sportmonks_id = models.CharField(max_length=32, blank=True, null=True)
    
    class Meta:
        ordering = ['minute', 'added_time', 'id']
        verbose_name = "Событие матча"
        verbose_name_plural = "События матча"
        indexes = [
            # match.events.filter(...).order_by('minute', ...) (страница
            # матча, events/views.py::pulse_partial) и апсерт по
            # (match, minute, event_type, team_side) в parsers/kff/
            # importers.py::import_events_and_minutes раньше шли full scan —
            # ни на match, ни на minute индекса не было (в отличие от
            # EventReaction, у которой event_reaction_type_idx есть с самого
            # начала). При росте базы матчей это первый кандидат на
            # деградацию страницы матча. Явное имя — тот же принцип, что и
            # у event_reaction_type_idx выше (ручные миграции без доступа
            # к реальной БД для makemigrations).
            models.Index(fields=['match', 'minute'], name='match_event_match_minute_idx'),
            # Поиск "то же самое событие Sportmonks при повторном импорте"
            # (см. sportmonks_id выше и import_events) — по (match,
            # sportmonks_id), не глобально, т.к. id уникален у Sportmonks
            # только в пределах их системы, а не гарантированно в нашей БД.
            models.Index(fields=['match', 'sportmonks_id'], name='match_event_sportmonks_id_idx'),
        ]
    
    def __str__(self):
        return f"{self.minute}' {self.get_event_type_display()} - {self.player_display_name or self.player}"

    @property
    def display_minute(self):
        """Форматирует минуту с добавлением времени"""
        if self.added_time:
            return f"{self.minute}+{self.added_time}"
        return str(self.minute)

    # 2026-09-21 (жалоба пользователя — скриншот события матча "45' Гол" без
    # имени забившего): `player`/`assist_player` — FK на `players.Player` по
    # sportmonks_id (см. parsers/sportmonks/importers.py::import_events), и
    # если наш локальный поиск по sportmonks_id не находит совпадения (игрок
    # ещё не успел попасть в базу через составы — например, поздняя замена,
    # чьи lineup-данные ещё не долетели к моменту события, или расхождение
    # ID у источника), FK молча остаётся NULL — вся лента событий и live-пуш
    # показывали пустое место вместо имени, хотя само имя РЕАЛЬНО есть в
    # ответе Sportmonks (`player_name`/`related_player_name` — подтверждено
    # документацией API, см. docs.sportmonks.com/v3 events include) и уже
    # сохраняется целиком в `extra_data` (см. import_events: `extra_data=evt`),
    # просто раньше нигде не читалось оттуда. Эти два свойства — единая точка
    # чтения "имя для отображения": сначала настоящий `Player` (даёт ссылку
    # на профиль в шаблоне), если его нет — сырая строка из ответа API (без
    # ссылки, но хотя бы не пусто), и только если нет вообще ничего — None
    # (шаблон/пуш сами решают, что показать в этом крайнем случае).
    @property
    def player_display_name(self) -> str | None:
        if self.player_id:
            return str(self.player)
        return (self.extra_data or {}).get('player_name') or None

    @property
    def assist_display_name(self) -> str | None:
        if self.assist_player_id:
            return str(self.assist_player)
        return (self.extra_data or {}).get('related_player_name') or None

    @property
    def player_out_display_name(self) -> str | None:
        """Для замен: `related_player_name` из Sportmonks относится к
        игроку, УШЕДШЕМУ с поля (см. import_events — player_id/`player`
        это вышедший НА поле, related_player_id/`player_out` — ушедший),
        та же сырая строка, что читает assist_display_name для голов, но
        под своим именем здесь — семантика разная (ассист vs "кто ушёл"),
        хотя исходное поле API одно и то же."""
        if self.player_out_id:
            return str(self.player_out)
        return (self.extra_data or {}).get('related_player_name') or None


class EventReaction(BaseModel):
    """
    Продуктовый аудит, раздел 2 ("Live-слой"): лёгкая реакция 👍/👎 на
    КОНКРЕТНОЕ событие матча (гол, карточка, VAR) в момент, когда оно
    происходит — в отличие от полного 6-шагового вайзарда оценки, который
    физически можно пройти только ПОСЛЕ финального свистка. Один тап,
    без формы, без логики антифрода уровня вайзарда (см. `_flag_if_
    suspicious` в `evaluations/views.py`) — цена намеренно низкая, чтобы
    не отпугнуть зрителя, реагирующего "на бегу" во время трансляции.

    ПОЧЕМУ ОТДЕЛЬНАЯ МОДЕЛЬ, А НЕ ПЕРЕИСПОЛЬЗОВАНИЕ `evaluations.*`: любая
    из моделей evaluations жёстко привязана к завершённому матчу через
    `EvaluationSession`/`unique_...` констрейнты "один раз на пользователя
    за матч" — реакции же множественные (можно тапнуть 👍 на гол, 👎 на
    следующую жёлтую карточку), по одной на каждое СОБЫТИЕ, а не на матч
    целиком, и не должны участвовать в расчёте `performance_score`
    (`aggregates/services.py`) — это разные по природе сигналы: реакция
    "на эмоции сейчас" vs. взвешенная оценка "вклад/риск/потенциал"
    постфактум.
    """
    REACTION_CHOICES = [
        ("like", "👍"),
        ("dislike", "👎"),
    ]

    match_event = models.ForeignKey(
        'events.MatchEvent',
        on_delete=models.CASCADE,
        related_name='reactions',
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='event_reactions',
    )
    reaction = models.CharField(max_length=10, choices=REACTION_CHOICES)

    class Meta:
        verbose_name = "Реакция на событие"
        verbose_name_plural = "Реакции на события"
        constraints = [
            # Один пользователь — одна реакция на одно событие (тап по
            # 👎 после 👍 ЗАМЕНЯЕТ реакцию через update_or_create в
            # events/services.py, а не создаёт вторую строку).
            models.UniqueConstraint(fields=['match_event', 'user'], name='unique_event_reaction')
        ]
        indexes = [
            # Явное имя — не полагаемся на автогенерируемый Django хеш
            # (миграции в этом проекте пишутся вручную без доступа к
            # реальной БД для `makemigrations`, см. analytics/models.py
            # для прецедента с models.E034 на автогенерированных именах).
            models.Index(fields=['match_event', 'reaction'], name='event_reaction_type_idx'),
        ]

    def __str__(self):
        return f"{self.user.username} {self.get_reaction_display()} → {self.match_event_id}"