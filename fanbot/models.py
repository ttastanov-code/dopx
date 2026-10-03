# fanbot/models.py
"""Привязка аккаунтов болельщиков к Telegram: вход, уведомления, Mini App."""
import secrets
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

LINK_CODE_TTL = timedelta(minutes=15)


class TelegramAccount(models.Model):
    """Аккаунт DOPX ↔ Telegram по числовому ID. can_message — бот может писать (пользователь его запустил)."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="telegram",
                                verbose_name=_("Пользователь"))
    telegram_id = models.BigIntegerField(_("Telegram ID"), unique=True)
    username = models.CharField(_("Username в Telegram"), max_length=64, blank=True)
    first_name = models.CharField(_("Имя"), max_length=128, blank=True)
    photo_url = models.URLField(_("Фото"), max_length=500, blank=True)
    can_message = models.BooleanField(_("Бот может писать"), default=False)
    notify = models.BooleanField(_("Уведомления в Telegram"), default=True)
    linked_at = models.DateTimeField(_("Привязан"), auto_now_add=True)
    last_seen = models.DateTimeField(_("Последний вход"), null=True, blank=True)

    class Meta:
        verbose_name = _("Telegram болельщика")
        verbose_name_plural = _("Telegram болельщиков")

    def __str__(self):
        return f"{self.user} ↔ {self.telegram_id}"


def _code() -> str:
    return secrets.token_urlsafe(12)


class TelegramLinkCode(models.Model):
    """Одноразовый код для ссылки t.me/<бот>?start=link_<код> — привязать Telegram к уже существующему аккаунту."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    code = models.CharField(max_length=32, unique=True, default=_code)
    created_at = models.DateTimeField(auto_now_add=True)
    used_at = models.DateTimeField(null=True, blank=True)

    def is_valid(self) -> bool:
        return self.used_at is None and timezone.now() - self.created_at < LINK_CODE_TTL
