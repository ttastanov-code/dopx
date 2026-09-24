# events/models.py
from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _
from core.models import BaseModel
from matches.models import Match
from players.models import Player


class MatchEvent(BaseModel):
    """Событие матча."""
    
    EVENT_TYPES = [
        ("goal", _("Гол")),
        ("yellow_card", _("Жёлтая карточка")),
        ("red_card", _("Красная карточка")),
        ("substitution", _("Замена")),
        ("penalty", _("Пенальти")),
        ("own_goal", _("Автогол")),
        ("var_check", _("VAR проверка")),
        # Отменённый гол (в т.ч. по VAR) — нужен для пуша «гол отменён».
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
    
    # Игрок события
    player = models.ForeignKey(
        'players.Player',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='events'
    )
    
    # Детализация:
    
    # Голы: ассистент
    assist_player = models.ForeignKey(
        'players.Player',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='assists'
    )
    
    # Голы: счёт после гола
    score_after = models.CharField(
        max_length=5,
        blank=True,
        null=True,
        help_text="Например: 2-1"
    )
    
    # Замены: ушедший игрок
    player_out = models.ForeignKey(
        'players.Player',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='substitutions_out'
    )
    
    # Карточки: причина
    card_reason = models.CharField(
        max_length=30,
        choices=CARD_REASONS,
        blank=True,
        null=True
    )
    
    # VAR: результат проверки
    var_decision = models.CharField(
        max_length=50,
        blank=True,
        help_text="Решение после VAR"
    )
    
    # Сырые данные события из API
    extra_data = models.JSONField(
        default=dict,
        blank=True,
        help_text="Сырые данные из API"
    )

    # ID события в Sportmonks — повторный импорт обновляет ту же запись (VAR меняет тип).
    sportmonks_id = models.CharField(max_length=32, blank=True, null=True)
    
    class Meta:
        ordering = ['minute', 'added_time', 'id']
        verbose_name = "Событие матча"
        verbose_name_plural = "События матча"
        indexes = [
            # Индекс для выборки событий матча по минуте.
            models.Index(fields=['match', 'minute'], name='match_event_match_minute_idx'),
            # Уникальность sportmonks_id — в пределах матча.
            models.Index(fields=['match', 'sportmonks_id'], name='match_event_sportmonks_id_idx'),
        ]
    
    def __str__(self):
        return f"{self.minute}' {self.get_event_type_display()} - {self.player_display_name or self.player}"

    @property
    def display_minute(self):
        """Минута с добавленным временем."""
        if self.added_time:
            return f"{self.minute}+{self.added_time}"
        return str(self.minute)

    # Имя для отображения: Player, если найден, иначе строка из extra_data, иначе None.
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
        """Для замен related_player_name — ушедший игрок."""
        if self.player_out_id:
            return str(self.player_out)
        return (self.extra_data or {}).get('related_player_name') or None


class EventReaction(BaseModel):
    """Реакция 👍/👎 на конкретное событие во время матча.
    Отдельно от evaluations: реакций много на матч, в performance_score не входят.
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
            # Одна реакция пользователя на событие (update_or_create).
            models.UniqueConstraint(fields=['match_event', 'user'], name='unique_event_reaction')
        ]
        indexes = [
            # Явное имя индекса.
            models.Index(fields=['match_event', 'reaction'], name='event_reaction_type_idx'),
        ]

    def __str__(self):
        return f"{self.user.username} {self.get_reaction_display()} → {self.match_event_id}"