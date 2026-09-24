# parsers/models.py
"""Модели парсера: журнал синков, расхождения импорта, проверка ФИО через ИИ."""
from __future__ import annotations

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.core.cache import cache
from django.db import models
from django.utils.translation import gettext_lazy as _

from core.models import BaseModel


class ParserSyncRun(BaseModel):
    """Один запуск задачи синка. Пишется один раз в конце задачи."""

    # Источник данных (старые строки — kff).
    SOURCE_CHOICES = [
        ("kff", "KFF"),
        ("sportmonks", "Sportmonks"),
    ]
    source = models.CharField(_('Источник'), max_length=20, choices=SOURCE_CHOICES, default="kff", db_index=True)

    # created_at — момент завершения, started_at — для длительности.
    task_name = models.CharField(_('Задача'), max_length=100, db_index=True)
    started_at = models.DateTimeField(_('Начало'))

    total = models.PositiveIntegerField(_('Всего матчей'), default=0)
    updated = models.PositiveIntegerField(_('Обновлено'), default=0)
    unchanged = models.PositiveIntegerField(_('Без изменений'), default=0)
    errors = models.PositiveIntegerField(_('Ошибок'), default=0)
    new_events = models.PositiveIntegerField(_('Новых событий'), default=0)
    status_changes = models.PositiveIntegerField(_('Смен статуса'), default=0)
    skipped_locked = models.PositiveIntegerField(_('Пропущено (лок)'), default=0)

    # Короткие сэмплы ошибок, полный traceback — в логе.
    error_samples = models.JSONField(_('Сэмплы ошибок'), default=list, blank=True)

    class Meta:
        verbose_name = _('Запуск синхронизации')
        verbose_name_plural = _('Запуски синхронизации')
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['task_name', '-created_at'], name='parser_run_task_time_idx'),
        ]

    def __str__(self):
        return f"{self.task_name} @ {self.created_at:%Y-%m-%d %H:%M} ({self.errors} ошибок из {self.total})"

    @property
    def duration_seconds(self) -> float:
        return (self.created_at - self.started_at).total_seconds()

    @property
    def error_rate_percent(self) -> float:
        if not self.total:
            return 0.0
        return round(self.errors / self.total * 100, 1)


class ParserDiscrepancy(BaseModel):
    """Источник задним числом поменял счёт/статус завершённого матча.
    Пишет import_match_core; staff разбирает вручную (reviewed/note).
    """

    match = models.ForeignKey(
        'matches.Match',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='parser_discrepancies',
        verbose_name=_('Матч'),
    )
    # Снимок названия матча.
    match_label = models.CharField(_('Матч (снэпшот)'), max_length=200)
    field_name = models.CharField(_('Поле'), max_length=50)
    old_value = models.CharField(_('Было'), max_length=200)
    new_value = models.CharField(_('Стало'), max_length=200)

    reviewed = models.BooleanField(_('Разобрано'), default=False, db_index=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='+',
        verbose_name=_('Кто разобрал'),
    )
    reviewed_at = models.DateTimeField(_('Когда разобрано'), null=True, blank=True)
    note = models.TextField(_('Заметка'), blank=True)

    class Meta:
        verbose_name = _('Расхождение импорта')
        verbose_name_plural = _('Расхождения импорта')
        ordering = ['reviewed', '-created_at']
        indexes = [
            models.Index(fields=['reviewed', '-created_at'], name='parser_discrepancy_review_idx'),
        ]

    FIELD_LABELS = {"home_score": "Голы хозяев", "away_score": "Голы гостей", "status": "Статус"}

    @property
    def field_label(self):
        return self.FIELD_LABELS.get(self.field_name, self.field_name)

    def __str__(self):
        return f"{self.match_label}: {self.field_name} {self.old_value} → {self.new_value}"


# ============================================================
# Проверка ФИО через ИИ (parsers/name_ai.py)
# ============================================================

class NameVerificationSuggestion(BaseModel):
    """Предложение ИИ по имени Player/Referee/Coach — очередь на ручную проверку.
    Generic FK, как у SuspiciousActivityFlag.
    """

    STATUS_CHOICES = [
        ("pending_review", _("Ждёт проверки staff")),
        ("approved", _("Подтверждено")),
        ("rejected", _("Отклонено")),
        ("check_failed", _("Ошибка запроса к Gemini")),
    ]
    CONFIDENCE_CHOICES = [
        ("high", _("Высокая")),
        ("medium", _("Средняя")),
        ("low", _("Низкая")),
    ]
    ENTITY_LABEL_CHOICES = [
        ("player", _("Игрок")),
        ("referee", _("Судья")),
        ("coach", _("Тренер")),
    ]

    content_type = models.ForeignKey("contenttypes.ContentType", on_delete=models.CASCADE, verbose_name=_("Тип сущности"))
    object_id = models.CharField(_("ID сущности"), max_length=64)
    content_object = GenericForeignKey("content_type", "object_id")

    entity_label = models.CharField(_("Роль"), max_length=10, choices=ENTITY_LABEL_CHOICES)
    # Снимки — запись читаема, даже если сущность изменилась или удалена.
    sportmonks_id = models.CharField(_("Sportmonks ID (снэпшот)"), max_length=100, blank=True)
    current_first_name = models.CharField(_("Текущее имя"), max_length=120, blank=True)
    current_last_name = models.CharField(_("Текущая фамилия"), max_length=120, blank=True)

    suggested_first_name = models.CharField(_("Предложенное имя"), max_length=120, blank=True)
    suggested_last_name = models.CharField(_("Предложенная фамилия"), max_length=120, blank=True)
    confidence = models.CharField(_("Уверенность"), max_length=10, choices=CONFIDENCE_CHOICES, blank=True)
    reasoning = models.TextField(_("Обоснование от Gemini"), blank=True)
    matches_current = models.BooleanField(_("Gemini подтвердил текущее написание"), default=False)

    status = models.CharField(_("Статус"), max_length=20, choices=STATUS_CHOICES, default="pending_review", db_index=True)
    error_message = models.TextField(_("Ошибка запроса"), blank=True)

    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="reviewed_name_suggestions", verbose_name=_("Кто проверил"),
    )
    reviewed_at = models.DateTimeField(_("Когда проверено"), null=True, blank=True)

    class Meta:
        verbose_name = _("Предложение по ФИО (ИИ)")
        verbose_name_plural = _("Предложения по ФИО (ИИ)")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "-created_at"], name="name_suggestion_status_idx"),
            models.Index(fields=["content_type", "object_id"], name="name_suggestion_entity_idx"),
        ]
        constraints = [
            # Без дублей при повторном запуске verify_names_with_ai.
            models.UniqueConstraint(
                fields=["content_type", "object_id"], condition=models.Q(status="pending_review"),
                name="uniq_pending_review_per_entity",
            ),
        ]

    def __str__(self):
        return f"{self.get_entity_label_display()}: {self.current_first_name} {self.current_last_name} → {self.suggested_first_name} {self.suggested_last_name} ({self.status})"


class ConfirmedNameCorrection(BaseModel):
    """Подтверждённые поправки имён (DB-версия PLAYER_NAME_CORRECTIONS).
    Работают сразу, без деплоя. Ключ — неверный текст в нижнем регистре.
    """

    wrong_text = models.CharField(_("Неверный текст"), max_length=120, unique=True, db_index=True)
    correct_text = models.CharField(_("Верный текст"), max_length=120)
    source_suggestion = models.ForeignKey(
        NameVerificationSuggestion, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="confirmed_corrections", verbose_name=_("Из предложения ИИ"),
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name=_("Кто подтвердил"),
    )

    class Meta:
        verbose_name = _("Подтверждённая поправка ФИО")
        verbose_name_plural = _("Подтверждённые поправки ФИО")
        ordering = ["wrong_text"]

    def __str__(self):
        return f"{self.wrong_text!r} → {self.correct_text!r}"

    def save(self, *args, **kwargs):
        self.wrong_text = self.wrong_text.strip().lower()
        super().save(*args, **kwargs)
        # Сбрасываем кэш — поправка применяется со следующего импорта.
        cache.delete(_CORRECTIONS_CACHE_KEY)

    def delete(self, *args, **kwargs):
        super().delete(*args, **kwargs)
        cache.delete(_CORRECTIONS_CACHE_KEY)


_CORRECTIONS_CACHE_KEY = "parsers:confirmed_name_corrections"
_CORRECTIONS_CACHE_TTL_SECONDS = 300


def get_confirmed_corrections() -> dict:
    """Словарь поправок с кэшем на 5 минут; сбрасывается при save/delete."""
    cached = cache.get(_CORRECTIONS_CACHE_KEY)
    if cached is not None:
        return cached
    result = dict(ConfirmedNameCorrection.objects.values_list("wrong_text", "correct_text"))
    cache.set(_CORRECTIONS_CACHE_KEY, result, timeout=_CORRECTIONS_CACHE_TTL_SECONDS)
    return result
