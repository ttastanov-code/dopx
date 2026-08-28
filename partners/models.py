# partners/models.py
"""
Партнёрская инфраструктура (продуктовый аудит "канал привлечения",
2026-08-21): до этого приложения у DOPX не было ни одной сущности,
которую можно было бы показать потенциальному партнёру (медиа, клубный
паблик, микроинфлюенсер, букмекер) кроме "у нас классный продукт,
поверьте". Два объекта здесь закрывают это:

- Partner — кто с нами сотрудничает, для атрибуции трафика (см.
  partners/views.py::PartnerReferralRedirectView, роут /go/<slug>/).
- Banner — классический рекламный баннер по зонам сайта. Букмекеры —
  самый частый партнёр спортивных площадок и именно баннеры, а не API,
  их стандартный формат размещения.

Импрессии/клики НЕ хранятся отдельными моделями — переиспользуем
analytics.AnalyticsEvent (WIDGET_EMBED_VIEWED/PARTNER_REFERRAL_VISIT/
BANNER_IMPRESSION/BANNER_CLICK), см. partners/services.py и
partners/selectors.py. Заводить параллельные таблицы счётчиков означало
бы два источника истины для одной и той же продуктовой воронки.
"""
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
    """
    Партнёр — контрагент, с которым согласован обмен трафиком (embed-
    виджет на их площадке, реферальная ссылка, размещение баннера). Не
    путать с Banner.partner — один партнёр может стоять за несколькими
    баннерами и/или просто получать реферальную ссылку без баннера.
    """

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
    # Закрытый контент-фид (partners/views.py::PartnerContentFeedView) — не
    # публичный API поверх спарсенных у KFF данных (об этом риске явно
    # предупреждали при обсуждении партнёрской инфраструктуры), а токен-
    # защищённая выдача ГОТОВЫХ брендированных ассетов (картинка + подпись
    # с нашей аналитикой). editable=False — генерируется автоматически,
    # ротация токена = пересоздание объекта, а не редактирование поля вручную.
    feed_token = models.UUIDField(_("Токен контент-фида"), default=uuid.uuid4, unique=True, editable=False)

    class Meta:
        verbose_name = _("Партнёр")
        verbose_name_plural = _("Партнёры")
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class BannerZone(models.TextChoices):
    """
    Зоны размещения баннера. Не завязаны на конкретный шаблон намеренно —
    новую зону можно объявить здесь и подключить {% render_banner "zone" %}
    в любом шаблоне без изменения модели.
    """

    HOME_HERO = "home_hero", _("Главная — верх")
    SIDEBAR = "sidebar", _("Боковая колонка")
    MATCH_DETAIL = "match_detail", _("Страница матча")
    LEADERBOARD = "leaderboard", _("Лидерборд")


class Banner(BaseModel):
    """Рекламный баннер. Ротация по зоне — см. partners/services.py::get_active_banner_for_zone."""

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
    # Букмекеры — самый частый тип партнёра спортивных площадок, и именно
    # к гэмблинг-рекламе в РК применяются требования об обязательной
    # пометке "18+"/предупреждении о риске зависимости — это и был исходный
    # повод завести поле. Но само поле и текст дисклеймера (см.
    # templates/components/_banner.html) намеренно НЕ привязаны к гэмблингу
    # формулировкой — под пометку 18+ может попасть любой другой контент
    # (алкоголь, табак и т.д.), а не только букмекеры. Поле явное, а не
    # выведенное из partner.partner_type == BOOKMAKER — баннер могут
    # разместить и без привязанного Partner, ответственность за пометку
    # не должна тихо теряться в этом случае.
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
        """
        is_active + внутри окна starts_at/ends_at (оба необязательны) +
        если баннер привязан к партнёру — партнёр тоже должен быть активен.

        БАГ, КОТОРЫЙ ТУТ БЫЛ (найден при написании тестов на partners,
        2026-08-28): метод проверял только собственные поля баннера —
        Partner.is_active вообще не участвовал в решении. Деактивация
        партнёра в админке (контракт закончился, партнёр нарушил условия
        размещения, площадка попросила снять рекламу) НЕ останавливала уже
        включённые баннеры: partners/services.py::get_active_banner_for_zone
        продолжал их показывать (и накручивать показы/клики в статистике)
        до тех пор, пока staff не находил и не выключал КАЖДЫЙ баннер
        этого партнёра вручную по отдельности — единственный переключатель
        "Активен" на самом Partner был чисто декоративным для уже
        размещённой рекламы. partner=None (собственное промо DOPX без
        привязки к партнёру, см. Banner.partner.help_text) этой проверкой
        не затрагивается — устанавливать/снимать его может только staff
        через Banner.is_active напрямую.
        """
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
