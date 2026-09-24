# partners/models.py
"""Партнёры и баннеры. Показы/клики хранятся в analytics.AnalyticsEvent."""
from __future__ import annotations

import uuid

from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from core.models import BaseModel


class PartnerType(models.TextChoices):
    MEDIA = "media", _("Спортивное медиа")
    CLUB = "club", _("Клубный паблик")
    INFLUENCER = "influencer", _("Микроинфлюенсер")
    BOOKMAKER = "bookmaker", _("Букмекер")
    OTHER = "other", _("Другое")


class Partner(BaseModel):
    """Партнёр: виджет, реферальная ссылка, баннеры."""

    name = models.CharField(_("Название"), max_length=150)
    slug = models.SlugField(
        _("Слаг"), unique=True, max_length=60,
        help_text=_("Используется в реферальной ссылке /go/<slug>/"),
    )
    partner_type = models.CharField(
        _("Тип"), max_length=20, choices=PartnerType.choices, default=PartnerType.OTHER,
    )
    contact_name = models.CharField(_("Контактное лицо"), max_length=150, blank=True)
    contact_email = models.EmailField(_("Email"), blank=True)
    website = models.URLField(_("Сайт"), blank=True)
    is_active = models.BooleanField(_("Активен"), default=True)
    notes = models.TextField(
        _("Заметки"), blank=True,
        help_text=_("Внутренние заметки по договорённости — не показываются публично"),
    )
    # Токен закрытого контент-фида. Генерируется автоматически.
    feed_token = models.UUIDField(_("Токен контент-фида"), default=uuid.uuid4, unique=True, editable=False)

    class Meta:
        verbose_name = _("Партнёр")
        verbose_name_plural = _("Партнёры")
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class BannerZone(models.TextChoices):
    """Зоны баннеров: {% render_banner "zone" %} в любом шаблоне."""

    HOME_HERO = "home_hero", _("Главная — верх")
    SIDEBAR = "sidebar", _("Боковая колонка")
    MATCH_DETAIL = "match_detail", _("Страница матча")
    LEADERBOARD = "leaderboard", _("Лидерборд")


class Banner(BaseModel):
    """Рекламный баннер. Ротация по зоне — get_active_banner_for_zone."""

    partner = models.ForeignKey(
        Partner, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="banners", verbose_name=_("Партнёр"),
        help_text=_("Необязательно — баннер может быть собственным промо DOPX без привязки к партнёру"),
    )
    zone = models.CharField(_("Зона показа"), max_length=20, choices=BannerZone.choices, db_index=True)
    title = models.CharField(
        _("Название"), max_length=150,
        help_text=_("Внутреннее название + alt-текст картинки, пользователю не показывается отдельно"),
    )
    image = models.ImageField(_("Изображение"), upload_to="banners/%Y/%m/")
    target_url = models.URLField(_("Ссылка перехода"))
    is_active = models.BooleanField(_("Активен"), default=True)
    starts_at = models.DateTimeField(_("Показывать с"), null=True, blank=True)
    ends_at = models.DateTimeField(_("Показывать до"), null=True, blank=True)
    priority = models.PositiveIntegerField(
        _("Приоритет"), default=0,
        help_text=_("Выше число — чаще показывается среди активных баннеров той же зоны"),
    )
    # Пометка 18+ — задаётся явно на баннере.
    requires_age_disclaimer = models.BooleanField(
        _("Требует пометки 18+"), default=False,
        help_text=_("Для любого контента 18+ (букмекеры/гэмблинг, алкоголь, табак и т.п.) — под баннером покажется дисклеймер"),
    )

    class Meta:
        verbose_name = _("Баннер")
        verbose_name_plural = _("Баннеры")
        ordering = ["-priority", "-created_at"]
        indexes = [
            models.Index(fields=["zone", "is_active"]),
        ]

    def __str__(self) -> str:
        return f"{self.title} ({self.get_zone_display()})"

    def is_currently_active(self) -> bool:
        """Активен: is_active, в окне starts_at/ends_at, и партнёр (если есть) тоже активен."""
        if not self.is_active:
            return False
        if self.partner_id and not self.partner.is_active:
            return False
        now = timezone.now()
        if self.starts_at and now < self.starts_at:
            return False
        if self.ends_at and now > self.ends_at:
            return False
        return True
