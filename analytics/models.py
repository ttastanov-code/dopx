# analytics/models.py
"""Продуктовая аналитика (воронка: визит -> регистрация -> оценка -> шеринг).
AnalyticsEvent без BaseModel: BigAutoField и без updated_at — append-only таблица.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class EventName(models.TextChoices):
    """Каталог событий. event_name — только через этот Enum."""

    PAGE_VIEW = "page_view", _("Просмотр страницы")
    USER_REGISTERED = "user_registered", _("Регистрация")
    USER_LOGIN = "user_login", _("Вход")
    WIZARD_STARTED = "wizard_started", _("Начало оценки матча")
    WIZARD_STEP_COMPLETED = "wizard_step_completed", _("Завершён шаг оценки")
    WIZARD_ABANDONED = "wizard_abandoned", _("Оценка брошена")
    EVALUATION_COMPLETED = "evaluation_completed", _("Оценка матча завершена")
    SHARE_CARD_VIEWED = "share_card_viewed", _("Просмотр шер-карточки")
    SHARE_CLICKED = "share_clicked", _("Клик 'Поделиться'")
    PROFILE_VIEWED = "profile_viewed", _("Просмотр публичного профиля")
    LEADERBOARD_VIEWED = "leaderboard_viewed", _("Просмотр лидерборда")
    # Прогноз 1X2 (и новый, и смена).
    PREDICTION_MADE = "prediction_made", _("Прогноз на матч сделан")
    # Партнёры: показы/клики баннеров, данные — в properties.
    WIDGET_EMBED_VIEWED = "widget_embed_viewed", _("Открытие embed-виджета")
    PARTNER_REFERRAL_VISIT = "partner_referral_visit", _("Переход по партнёрской ссылке")
    BANNER_IMPRESSION = "banner_impression", _("Показ баннера")
    BANNER_CLICK = "banner_click", _("Клик по баннеру")
    PARTNER_FEED_ACCESSED = "partner_feed_accessed", _("Запрос партнёрского контент-фида")


class AnalyticsEvent(models.Model):
    """Событие аналитики."""

    id = models.BigAutoField(primary_key=True)
    created_at = models.DateTimeField(_("Создано"), auto_now_add=True, db_index=True)
    event_name = models.CharField(_("Событие"), max_length=50, choices=EventName.choices, db_index=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="analytics_events", verbose_name=_("Пользователь"),
        help_text=_("SET_NULL: агрегаты должны переживать удаление аккаунта"),
    )
    # UUID из localStorage — связывает анонимный визит и события после входа.
    anonymous_id = models.UUIDField(_("Анонимный ID"), null=True, blank=True, db_index=True)
    session_id = models.CharField(max_length=40, blank=True)
    properties = models.JSONField(_("Свойства"), default=dict, blank=True)
    url_path = models.CharField(max_length=500, blank=True)
    referrer = models.CharField(max_length=500, blank=True)
    utm_source = models.CharField(max_length=100, blank=True)
    utm_medium = models.CharField(max_length=100, blank=True)
    utm_campaign = models.CharField(max_length=100, blank=True)
    ip_hash = models.CharField(
        max_length=64, blank=True,
        help_text=_("SHA-256(IP+SECRET_KEY) — сырой IP никогда не пишем, см. analytics.services.hash_ip"),
    )
    user_agent = models.CharField(max_length=300, blank=True)

    class Meta:
        verbose_name = _("Событие аналитики")
        verbose_name_plural = _("События аналитики")
        indexes = [
            # Явные имена индексов.
            models.Index(fields=["event_name", "created_at"], name="analytics_event_created_idx"),
            models.Index(fields=["user", "created_at"], name="analytics_user_created_idx"),
            models.Index(fields=["anonymous_id", "created_at"], name="analytics_anon_created_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.event_name} @ {self.created_at:%Y-%m-%d %H:%M}"
