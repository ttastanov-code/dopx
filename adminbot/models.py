# adminbot/models.py
"""Модели бота: привязки сотрудников, разовые события, инциденты, посты канала. В dev и prod — свои."""
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
    topics = models.JSONField(_("Темы уведомлений"), null=True, blank=True)  # None — темы по умолчанию
    # 0 — не дежурит; 1 — первый получает инциденты, 2 — следующий и т.д.
    duty_order = models.PositiveSmallIntegerField(_("Очередь дежурства"), default=0)
    linked_at = models.DateTimeField(_("Привязан"), auto_now_add=True)
    last_seen = models.DateTimeField(_("Последнее действие"), null=True, blank=True)

    class Meta:
        verbose_name = _("Привязка к боту")
        verbose_name_plural = _("Привязки к боту")

    def __str__(self):
        return f"{self.user} ↔ {self.telegram_id}"

    def wants(self, topic: str | None) -> bool:
        return self.notify and (topic is None or topic in (DEFAULT_TOPICS if self.topics is None else self.topics))


class BotLinkCode(models.Model):
    """Одноразовый код из дашборда для команды /link."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    code = models.CharField(max_length=8, unique=True, default=_code)
    created_at = models.DateTimeField(auto_now_add=True)
    used_at = models.DateTimeField(null=True, blank=True)

    def is_valid(self) -> bool:
        return self.used_at is None and timezone.now() - self.created_at < LINK_CODE_TTL


# Темы уведомлений: сотрудник включает нужные в боте.
TOPICS = {
    "matchday": "брифинг перед игровым днём, финалы, отчёты после матча",
    "live": "старт матчей и голы",
    "incidents": "сбои сервера, всплески оценок, матч не начался",
    "queues": "мнения экспертов, флаги, обращения, расхождения данных",
    "channel": "посты в канал на одобрение",
    "digest": "утренняя сводка за вчера",
    "weekly": "недельный отчёт по команде (суперпользователю)",
}
DEFAULT_TOPICS = [t for t in TOPICS if t != "live"]


class BotEvent(models.Model):
    """Отметка «уже отправлено»: один ключ — одно уведомление, даже из нескольких процессов."""

    key = models.CharField(max_length=160, unique=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    @classmethod
    def once(cls, key: str) -> bool:
        """True — событие новое (и теперь отмечено)."""
        from django.db import IntegrityError, transaction

        try:
            with transaction.atomic():
                cls.objects.create(key=key[:160])
            return True
        except IntegrityError:
            return False


class Incident(models.Model):
    """Инцидент: уходит дежурному, без реакции — следующему; «Беру» видят все получатели."""

    key = models.CharField(max_length=120, db_index=True)
    title = models.CharField(max_length=200)
    text = models.TextField()
    rows = models.JSONField(default=list, blank=True)
    section = models.CharField(max_length=40, default="system_status")
    perm = models.CharField(max_length=100, blank=True)
    level = models.PositiveSmallIntegerField(default=0)
    messages = models.JSONField(default=list, blank=True)
    acked_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    acked_at = models.DateTimeField(null=True, blank=True)
    next_escalation_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = _("Инцидент")
        verbose_name_plural = _("Инциденты")
        ordering = ["-created_at"]

    def __str__(self):
        return self.title


class ChannelPost(models.Model):
    """Пост в Telegram-канал проекта: черновик → (одобрение) → публикация."""

    KIND_CHOICES = [
        ("preview", _("Анонс тура")),
        ("result", _("Финал матча")),
        ("ratings", _("Оценки болельщиков")),
        ("round", _("Итоги тура")),
        ("expert", _("Мнение эксперта")),
        ("changes", _("Переносы и отмены")),
        ("manual", _("Свой пост")),
    ]
    STATUS_CHOICES = [
        ("draft", _("Черновик")),
        ("scheduled", _("Запланирован")),
        ("published", _("Опубликован")),
        ("failed", _("Ошибка")),
        ("cancelled", _("Удалён")),
    ]

    kind = models.CharField(_("Тип"), max_length=20, choices=KIND_CHOICES, default="manual")
    key = models.CharField(max_length=120, unique=True, null=True, blank=True)
    text = models.TextField(_("Текст"), max_length=4000)
    image = models.CharField(_("Картинка (путь в хранилище)"), max_length=300, blank=True)
    tg_file_id = models.CharField(max_length=200, blank=True)
    buttons = models.JSONField(_("Кнопки-ссылки"), default=list, blank=True)
    status = models.CharField(_("Статус"), max_length=12, choices=STATUS_CHOICES, default="draft", db_index=True)
    scheduled_at = models.DateTimeField(_("Опубликовать в"), null=True, blank=True)
    published_at = models.DateTimeField(_("Опубликован"), null=True, blank=True)
    message_id = models.BigIntegerField(null=True, blank=True)
    error = models.CharField(max_length=300, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    published_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Пост в канал")
        verbose_name_plural = _("Посты в канал")
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_kind_display()}: {self.text[:40]}"


class ChannelConfig(models.Model):
    """Режим автопостинга по типам: auto — сразу, approve — после одобрения, off — не готовить."""

    modes = models.JSONField(default=dict, blank=True)
    quiet_hours = models.BooleanField(_("Не публиковать ночью (23:00–09:00)"), default=True)
    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def get(cls) -> "ChannelConfig":
        return cls.objects.get_or_create(pk=1)[0]

    def mode(self, kind: str) -> str:
        return self.modes.get(kind, "approve")
