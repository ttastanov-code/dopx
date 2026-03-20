# events/models.py
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
    
    class Meta:
        ordering = ['minute', 'added_time', 'id']
        verbose_name = "Событие матча"
        verbose_name_plural = "События матча"
    
    def __str__(self):
        return f"{self.minute}' {self.get_event_type_display()} - {self.player}"
    
    @property
    def display_minute(self):
        """Форматирует минуту с добавлением времени"""
        if self.added_time:
            return f"{self.minute}+{self.added_time}"
        return str(self.minute)