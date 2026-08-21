# analytics/models.py
"""
Событийная аналитика продукта — единая точка данных для продуктовой
воронки (визит → регистрация → первая оценка → шеринг), которую сторонние
инструменты (GA4, Яндекс.Метрика) не знают в принципе, потому что не видят
доменных событий вроде "шаг вайзарда завершён" или "шер-карточка открыта".

AnalyticsEvent НЕ наследует core.models.BaseModel намеренно:
1. BigAutoField вместо UUID PK — append-only таблица с ожидаемым объёмом
   в десятки/сотни тысяч строк в месяц; UUID PK здесь даёт заметно худшую
   производительность вставки без единого практического плюса (событие
   никогда не адресуется по PK извне).
2. Нет updated_at — событие неизменяемо после записи, поле было бы мёртвым
   весом на каждой строке огромной таблицы.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class EventName(models.TextChoices):
    """
    Единый каталог событий продукта. Пишите event_name ТОЛЬКО через этот
    Enum — иначе через полгода в таблице будет 5 вариантов написания
    одного события ('signup', 'sign_up', 'user_registered') и воронка
    станет нечитаемой.
    """

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
    # 2026-08-21: краудсорс-прогноз 1X2 (predictions app). Один choice —
    # первая ставка И смена прогноза до старта матча (submit_prediction
    # использует update_or_create) — воронке для MVP достаточно факта
    # "пользователь взаимодействовал с прогнозами", не нужно различать эти
    # два случая отдельными event_name.
    PREDICTION_MADE = "prediction_made", _("Прогноз на матч сделан")


class AnalyticsEvent(models.Model):
    """Единичное событие продуктовой аналитики."""

    id = models.BigAutoField(primary_key=True)
    created_at = models.DateTimeField(_("Создано"), auto_now_add=True, db_index=True)
    event_name = models.CharField(_("Событие"), max_length=50, choices=EventName.choices, db_index=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="analytics_events", verbose_name=_("Пользователь"),
        help_text=_("SET_NULL: агрегаты должны переживать удаление аккаунта"),
    )
    # Клиентский UUID из localStorage — единственная связка между анонимным
    # визитом ДО регистрации и залогиненными событиями ПОСЛЕ. Без него
    # невозможна воронка "визит → регистрация → первая оценка".
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
            # ПРИМЕЧАНИЕ: имена индексов заданы явно (а не оставлены на
            # авто-хэш Django), потому что миграция 0001_initial написана
            # вручную (в песочнице разработки нет сетевого доступа к
            # PyPI/Postgres для `manage.py makemigrations` — см. коммент в
            # самой миграции) и должна детерминированно совпадать с
            # состоянием модели без раунд-трипа через реальный Django.
            models.Index(fields=["event_name", "created_at"], name="analytics_event_created_idx"),
            models.Index(fields=["user", "created_at"], name="analytics_user_created_idx"),
            models.Index(fields=["anonymous_id", "created_at"], name="analytics_anon_created_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.event_name} @ {self.created_at:%Y-%m-%d %H:%M}"
