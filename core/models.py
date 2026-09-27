import uuid
from django.db import models
from django.conf import settings
from django.utils.translation import gettext_lazy as _


class BaseModel(models.Model):

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


# Источник ФИО для Player/Referee/Coach (ставит _resolve_cyrillic_name):
#   clean_source            — чистая кириллица от Sportmonks;
#   latin_foreign           — иностранное имя, оставлена латиница;
#   guessed_transliteration — наша транслитерация (приоритет для проверки ИИ);
#   ai_verified             — проверено ИИ и одобрено staff;
#   manual                  — правка вручную.
# Лежит в core, чтобы не было циклического импорта.
NAME_SOURCE_CLEAN_SOURCE = "clean_source"
NAME_SOURCE_LATIN_FOREIGN = "latin_foreign"
NAME_SOURCE_GUESSED_TRANSLITERATION = "guessed_transliteration"
NAME_SOURCE_AI_VERIFIED = "ai_verified"
NAME_SOURCE_MANUAL = "manual"

NAME_SOURCE_CHOICES = [
    (NAME_SOURCE_CLEAN_SOURCE, _("Кириллица от источника (Sportmonks)")),
    (NAME_SOURCE_LATIN_FOREIGN, _("Иностранное имя, оставлено латиницей")),
    (NAME_SOURCE_GUESSED_TRANSLITERATION, _("Угадано нашей транслитерацией")),
    (NAME_SOURCE_AI_VERIFIED, _("Проверено ИИ, подтверждено staff")),
    (NAME_SOURCE_MANUAL, _("Правлено вручную")),
]


# PlatformSetting — рантайм-настройки (ключ/значение), правятся в дашборде без деплоя.
# Читать только через get_setting().
class PlatformSetting(models.Model):
    """Настройка платформы. Не для секретов."""

    TYPE_STRING = "string"
    TYPE_INT = "int"
    TYPE_FLOAT = "float"
    TYPE_BOOL = "bool"
    TYPE_CHOICES = [
        (TYPE_STRING, _("Текст")),
        (TYPE_INT, _("Целое число")),
        (TYPE_FLOAT, _("Дробное число")),
        (TYPE_BOOL, _("Да/Нет")),
    ]

    # Явный BigAutoField — короткий id, внешних ссылок нет.
    id = models.BigAutoField(primary_key=True)
    key = models.CharField(_("Ключ"), max_length=100, unique=True)
    value = models.TextField(_("Значение"), blank=True)
    value_type = models.CharField(_("Тип"), max_length=10, choices=TYPE_CHOICES, default=TYPE_STRING)
    description = models.TextField(_("Описание"), blank=True, help_text=_("Что это значение делает и на что влияет — показывается прямо в форме редактирования."))
    updated_at = models.DateTimeField(_("Изменено"), auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name=_("Кем изменено"),
    )

    class Meta:
        verbose_name = _("Настройка платформы")
        verbose_name_plural = _("Настройки платформы")
        ordering = ["key"]

    def __str__(self) -> str:
        return f"{self.key} = {self.value!r}"

    def typed_value(self):
        """Строка -> тип по value_type. Некорректное значение -> 0/0.0/False."""
        raw = (self.value or "").strip()
        if self.value_type == self.TYPE_BOOL:
            return raw.lower() in ("1", "true", "yes", "on", "да")
        if self.value_type == self.TYPE_INT:
            try:
                return int(raw)
            except ValueError:
                return 0
        if self.value_type == self.TYPE_FLOAT:
            try:
                return float(raw)
            except ValueError:
                return 0.0
        return raw


PLATFORM_SETTING_CACHE_TTL = 60  # сек


def get_setting(key: str, default=None):
    """Чтение настройки с кэшем и fallback на default."""
    from django.core.cache import cache

    cache_key = f"platform_setting:{key}"
    cached = cache.get(cache_key)
    if cached is not None:
        return default if cached == _MISSING_SENTINEL else cached

    try:
        setting = PlatformSetting.objects.get(key=key)
        value = setting.typed_value()
    except PlatformSetting.DoesNotExist:
        cache.set(cache_key, _MISSING_SENTINEL, PLATFORM_SETTING_CACHE_TTL)
        return default

    cache.set(cache_key, value, PLATFORM_SETTING_CACHE_TTL)
    return value


# Маркер «ключа нет в БД» — кэшируем и отсутствие.
# Строка, а не object(): после pickle через Redis identity теряется.
_MISSING_SENTINEL = "__platform_setting_missing__"



class MourningMode(models.Model):
    """Режим траура — одна запись (pk=1). Включение вручную или по периоду; см. core/mourning.py."""

    DEFAULT_MESSAGE = "День общенационального траура"

    is_enabled = models.BooleanField(_("Включён"), default=False)
    message = models.CharField(_("Текст плашки"), max_length=255, default=DEFAULT_MESSAGE)
    starts_at = models.DateTimeField(_("Начало"), null=True, blank=True, help_text=_("Пусто — сразу"))
    ends_at = models.DateTimeField(_("Окончание"), null=True, blank=True, help_text=_("Пусто — до ручного выключения"))
    grayscale = models.BooleanField(_("Монохромный сайт"), default=True)
    hide_ads = models.BooleanField(_("Скрыть рекламу и баннеры"), default=True)
    mute_push = models.BooleanField(_("Приглушить развлекательные push"), default=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
        verbose_name=_("Изменил"),
    )
    updated_at = models.DateTimeField(_("Изменено"), auto_now=True)

    class Meta:
        verbose_name = _("Режим траура")
        verbose_name_plural = _("Режим траура")

    def __str__(self):
        return f"Режим траура ({'включён' if self.is_enabled else 'выключен'})"

    def is_active_at(self, moment) -> bool:
        if not self.is_enabled:
            return False
        if self.starts_at and moment < self.starts_at:
            return False
        if self.ends_at and moment >= self.ends_at:
            return False
        return True
