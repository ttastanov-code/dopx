# players/models.py
from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _
from core.models import BaseModel, NAME_SOURCE_CHOICES
from teams.models import Team

class Player(BaseModel):
    """Футбольный игрок."""
    first_name = models.CharField(_('Имя'), max_length=120)
    last_name = models.CharField(_('Фамилия'), max_length=120)
    # Откуда взято ФИО (core/models.py::NAME_SOURCE_CHOICES). Пусто — неизвестно.
    name_source = models.CharField(
        _('Источник ФИО'), max_length=30, choices=NAME_SOURCE_CHOICES, blank=True, db_index=True,
    )
    team = models.ForeignKey(
        Team,
        on_delete=models.SET_NULL,
        null=True,
        related_name="players",
        verbose_name=_('Команда')
    )
    position = models.CharField(_('Позиция'), max_length=50, blank=True)
    number = models.IntegerField(_('Номер'), null=True, blank=True)
    photo = models.ImageField(_('Фото'), upload_to="players/", null=True, blank=True)
    # Фото из Sportmonks; обновляется синком. Загруженное вручную photo — в приоритете.
    photo_url = models.URLField(_('URL фото (Sportmonks)'), blank=True, default='')
    is_active = models.BooleanField(_('Активен'), default=True)
    external_id = models.CharField(
        _('Внешний ID'),
        max_length=100,
        unique=True,
        null=True,
        blank=True
    )
    # ID игрока на сайте kffleague.kz (для фото).
    kff_website_id = models.CharField(
        _('ID игрока на сайте KFF'),
        max_length=20, blank=True, null=True, unique=True,
        help_text=_('Числовой id из URL kffleague.kz/ru/player/<id> — для скрапинга фото.'),
    )
    # ID в Sportmonks.
    sportmonks_id = models.CharField(
        _('Sportmonks ID'),
        max_length=100, blank=True, null=True, unique=True,
    )
    # Счётчик отсутствий в составе на сайте KFF. Сейчас никем не обновляется.
    roster_absence_streak = models.PositiveIntegerField(
        _('Подряд отсутствовал в составе на сайте KFF'),
        default=0,
        help_text=_('Устаревшее поле старого скрапера KFF, больше не обновляется.'),
    )
    # Дата самого свежего матча, из которого взяты team/number/position.
    # Более старую фикстуру не применяем.
    last_match_at = models.DateTimeField(
        _('Дата последнего матча'),
        null=True, blank=True,
        help_text=_('Start_time самой свежей фикстуры, из которой обновлялись team/number/position — используется как защита от отката этих полей при бэкафилле не по хронологии.'),
    )

    class Meta:
        verbose_name = _('Игрок')
        verbose_name_plural = _('Игроки')
        ordering = ['last_name', 'first_name']
        # Явные имена индексов (см. миграцию 0007).
        indexes = [
            models.Index(fields=['team', 'is_active'], name='player_team_active_idx'),
            models.Index(fields=['last_name', 'first_name'], name='player_last_first_name_idx'),
        ]

    def __str__(self):
        return f"{self.first_name} {self.last_name}"

    @property
    def photo_display(self):
        """Фото для показа: ручная загрузка, иначе из Sportmonks."""
        return (self.photo.url if self.photo else None) or self.photo_url or None

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}"


class PlayerSidelined(BaseModel):
    """Травмы и дисквалификации (Sportmonks sidelined), синк раз в сутки.
    На is_active и историю оценок не влияет.
    """
    CATEGORY_CHOICES = [
        ("injury", _("Травма")),
        ("suspended", _("Дисквалификация")),
        ("other", _("Другое")),
    ]

    player = models.ForeignKey(
        Player,
        on_delete=models.CASCADE,
        related_name="sidelined_periods",
        verbose_name=_('Игрок'),
    )
    category = models.CharField(_('Категория'), max_length=20, choices=CATEGORY_CHOICES, default="other")
    start_date = models.DateField(_('С'), null=True, blank=True)
    end_date = models.DateField(_('По'), null=True, blank=True)
    sportmonks_id = models.CharField(
        _('Sportmonks ID'),
        max_length=100, blank=True, null=True, unique=True,
    )

    class Meta:
        verbose_name = _('Недоступность игрока')
        verbose_name_plural = _('Недоступность игроков')
        ordering = ['-start_date']
        indexes = [
            # Явное имя индекса.
            models.Index(fields=['player', 'end_date'], name='player_sidelined_end_date_idx'),
        ]

    def __str__(self):
        return f"{self.player} — {self.get_category_display()} ({self.start_date}–{self.end_date or '…'})"

    @property
    def is_current(self) -> bool:
        """Действует ли ограничение сейчас."""
        from django.utils import timezone
        today = timezone.now().date()
        if self.start_date and self.start_date > today:
            return False
        if self.end_date and self.end_date < today:
            return False
        return True


class PotentialDuplicatePlayer(BaseModel):
    """Возможный дубль игрока: в той же команде уже есть игрок с тем же ФИО,
    но другим sportmonks_id. Автоматически не сливается — разбор в дашборде.
    """

    existing_player = models.ForeignKey(
        'players.Player', on_delete=models.CASCADE, related_name='+',
        verbose_name=_('Уже существующий игрок'),
    )
    new_player = models.ForeignKey(
        'players.Player', on_delete=models.CASCADE, related_name='+',
        verbose_name=_('Новая запись (возможно, тот же человек)'),
    )
    # Снимок названия команды.
    team_label = models.CharField(_('Команда (снэпшот)'), max_length=200, blank=True)

    reviewed = models.BooleanField(_('Разобрано'), default=False, db_index=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
        verbose_name=_('Кто разобрал'),
    )
    reviewed_at = models.DateTimeField(_('Когда разобрано'), null=True, blank=True)
    note = models.TextField(_('Заметка'), blank=True, help_text=_('Например: "подтверждено, слил вручную" или "ложное срабатывание — разные люди".'))

    class Meta:
        verbose_name = _('Возможный дубль игрока')
        verbose_name_plural = _('Возможные дубли игроков')
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(fields=['existing_player', 'new_player'], name='potential_dup_player_unique_pair'),
        ]

    def __str__(self):
        return f"{self.existing_player} ↔ {self.new_player} ({self.team_label})"