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
    """Зоны баннеров: {% render_banner "zone" %} в любом шаблоне. Размеры — ZONE_SPECS."""

    HOME_HERO = "home_hero", _("Главная — верх")
    SIDEBAR = "sidebar", _("Главная — правая колонка")
    MATCH_DETAIL = "match_detail", _("Страница матча")
    LEADERBOARD = "leaderboard", _("Рейтинги пользователей и игроков")


# Размеры картинок по зонам: (ширина, высота) для компьютера и телефона.
# Рамка баннера держит эти пропорции, поэтому вёрстка не прыгает при загрузке.
ZONE_SPECS = {
    BannerZone.HOME_HERO: {"desktop": (1200, 300), "mobile": (720, 360), "where": "Над лентой матчей на главной, во всю ширину."},
    BannerZone.SIDEBAR: {"desktop": (600, 500), "mobile": (720, 600), "where": "Правая колонка главной, над турнирной таблицей."},
    BannerZone.MATCH_DETAIL: {"desktop": (600, 500), "mobile": (720, 600), "where": "Правая колонка страницы матча, над статистикой."},
    BannerZone.LEADERBOARD: {"desktop": (1456, 180), "mobile": (720, 240), "where": "Над таблицей рейтинга пользователей, городов и игроков."},
}


class BannerFormat(models.TextChoices):
    IMAGE = "image", _("Картинка")
    NATIVE = "native", _("Карточка (логотип, текст, кнопка)")


class BannerAudience(models.TextChoices):
    ALL = "all", _("Все посетители")
    GUESTS = "guests", _("Только гости")
    USERS = "users", _("Только вошедшие")


class Banner(BaseModel):
    """Рекламное размещение. Выбор — partners.services.pick_banner, показ считается по видимости."""

    partner = models.ForeignKey(
        Partner, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="banners", verbose_name=_("Партнёр"),
        help_text=_("Пусто — собственное промо DOPX"),
    )
    zone = models.CharField(_("Зона показа"), max_length=20, choices=BannerZone.choices, db_index=True)
    format = models.CharField(_("Формат"), max_length=10, choices=BannerFormat.choices, default=BannerFormat.IMAGE)
    title = models.CharField(_("Внутреннее название"), max_length=150, help_text=_("Видно только в управлении и как alt картинки"))
    advertiser = models.CharField(
        _("Рекламодатель"), max_length=120, blank=True,
        help_text=_("Показывается в пометке «Реклама · …». Пусто — название партнёра"),
    )
    target_url = models.URLField(_("Ссылка перехода"))

    # Картинка.
    image = models.ImageField(_("Картинка для компьютера"), upload_to="banners/%Y/%m/", blank=True)
    image_mobile = models.ImageField(
        _("Картинка для телефона"), upload_to="banners/%Y/%m/", blank=True,
        help_text=_("Необязательно. Без неё на телефоне покажется картинка для компьютера"),
    )
    # Карточка.
    logo = models.ImageField(_("Логотип"), upload_to="banners/logos/", blank=True)
    headline = models.CharField(_("Заголовок"), max_length=70, blank=True)
    body = models.CharField(_("Текст"), max_length=140, blank=True)
    cta_label = models.CharField(_("Текст кнопки"), max_length=24, blank=True, default="Подробнее")

    # Расписание и приоритет.
    is_active = models.BooleanField(_("Включён"), default=True)
    starts_at = models.DateTimeField(_("Показывать с"), null=True, blank=True)
    ends_at = models.DateTimeField(_("Показывать до"), null=True, blank=True)
    priority = models.PositiveIntegerField(
        _("Вес в ротации"), default=1,
        help_text=_("Если в зоне несколько баннеров, вес 3 показывается втрое чаще, чем вес 1"),
    )

    # Ограничения.
    audience = models.CharField(_("Аудитория"), max_length=10, choices=BannerAudience.choices, default=BannerAudience.ALL)
    max_impressions = models.PositiveIntegerField(_("Лимит показов"), null=True, blank=True, help_text=_("Пусто — без лимита"))
    max_clicks = models.PositiveIntegerField(_("Лимит кликов"), null=True, blank=True, help_text=_("Пусто — без лимита"))
    daily_cap_per_visitor = models.PositiveSmallIntegerField(
        _("Показов одному человеку в день"), default=0, help_text=_("0 — без ограничения"),
    )

    requires_age_disclaimer = models.BooleanField(
        _("Пометка 18+"), default=False,
        help_text=_("Для букмекеров, алкоголя, табака и другого контента 18+"),
    )

    class Meta:
        verbose_name = _("Баннер")
        verbose_name_plural = _("Баннеры")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["zone", "is_active"]),
        ]

    def __str__(self) -> str:
        return f"{self.title} ({self.get_zone_display()})"

    @property
    def advertiser_label(self) -> str:
        return self.advertiser or (self.partner.name if self.partner_id else "")

    @property
    def spec(self) -> dict:
        return ZONE_SPECS.get(self.zone, ZONE_SPECS[BannerZone.SIDEBAR])

    def is_ready(self) -> bool:
        """Хватает ли материалов для показа в выбранном формате."""
        if self.format == BannerFormat.NATIVE:
            return bool(self.headline)
        return bool(self.image)

    def status(self, totals: dict | None = None) -> str:
        """draft | paused | scheduled | running | finished | limit — для управления и выбора."""
        if not self.is_ready():
            return "draft"
        if not self.is_active or (self.partner_id and not self.partner.is_active):
            return "paused"
        now = timezone.now()
        if self.starts_at and now < self.starts_at:
            return "scheduled"
        if self.ends_at and now > self.ends_at:
            return "finished"
        if totals is not None and (
            (self.max_impressions and totals["impressions"] >= self.max_impressions)
            or (self.max_clicks and totals["clicks"] >= self.max_clicks)
        ):
            return "limit"
        return "running"

    def is_currently_active(self) -> bool:
        return self.status() == "running"


class BannerDailyStat(models.Model):
    """Показы и клики баннера за день. Показ — баннер был виден на экране не меньше секунды."""

    banner = models.ForeignKey(Banner, on_delete=models.CASCADE, related_name="daily_stats")
    date = models.DateField(_("Дата"), db_index=True)
    impressions = models.PositiveIntegerField(_("Показы"), default=0)
    clicks = models.PositiveIntegerField(_("Клики"), default=0)

    class Meta:
        verbose_name = _("Статистика баннера за день")
        verbose_name_plural = _("Статистика баннеров по дням")
        constraints = [models.UniqueConstraint(fields=["banner", "date"], name="unique_banner_daily_stat")]
        ordering = ["-date"]
