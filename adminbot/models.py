# adminbot/models.py
"""Привязка Telegram-аккаунтов сотрудников к боту. В каждом окружении (dev/prod) — своя."""
import secrets
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

# Срок жизни кода привязки.
LINK_CODE_TTL = timedelta(minutes=10)


def _code() -> str:
    return f"{secrets.randbelow(10 ** 8):08d}"


class BotLink(models.Model):
    """Сотрудник ↔ Telegram. По числовому ID: username можно сменить, ID — нет."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="bot_link",
                                verbose_name=_("Сотрудник"))
    telegram_id = models.BigIntegerField(_("Telegram ID"), unique=True)
    telegram_name = models.CharField(_("Имя в Telegram"), max_length=120, blank=True)
    notify = models.BooleanField(_("Присылать уведомления"), default=True)
    linked_at = models.DateTimeField(_("Привязан"), auto_now_add=True)
    last_seen = models.DateTimeField(_("Последнее действие"), null=True, blank=True)

    class Meta:
        verbose_name = _("Привязка к боту")
        verbose_name_plural = _("Привязки к боту")

    def __str__(self):
        return f"{self.user} ↔ {self.telegram_id}"


class BotLinkCode(models.Model):
    """Одноразовый код из дашборда для команды /link."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    code = models.CharField(max_length=8, unique=True, default=_code)
    created_at = models.DateTimeField(auto_now_add=True)
    used_at = models.DateTimeField(null=True, blank=True)

    def is_valid(self) -> bool:
        return self.used_at is None and timezone.now() - self.created_at < LINK_CODE_TTL
