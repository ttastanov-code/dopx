# dashboard/models.py
"""
Аудит-лог действий staff (продуктовый апгрейд, "защита на высшем уровне" —
не только КТО может зайти, но и КТО ЧТО сделал внутри). Django admin сам
пишет CRUD-изменения моделей в свою `django_admin_log` (LogEntry) — это
покрывает обычные add/change/delete через ModelAdmin автоматически и
переиспользуется как есть, БЕЗ дублирования здесь.

Но кастомные экшены staff-дашборда (dashboard/views.py, parser_tools.py) —
подтверждение/отклонение антифрод-флага, ручной ресинк матча, ручной запуск
celery-задачи — идут в обход ModelAdmin и в LogEntry не попадают вообще.
StaffActionLog закрывает именно этот пробел. См. dashboard/audit.py::log_staff_action.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class AuditAction(models.TextChoices):
    """Единый каталог экшенов — как и analytics.EventName, пишите action
    ТОЛЬКО через этот Enum, иначе через полгода в таблице будет разнобой
    в написании одного и того же действия."""

    ANTIFRAUD_FLAG_CONFIRMED = "antifraud_flag_confirmed", _("Флаг подтверждён")
    ANTIFRAUD_FLAG_DISMISSED = "antifraud_flag_dismissed", _("Флаг отклонён")
    MATCH_RESYNC = "match_resync", _("Ручной ресинк матча")
    CELERY_TASK_TRIGGERED = "celery_task_triggered", _("Запуск celery-задачи вручную")
    RAW_KFF_LOOKUP = "raw_kff_lookup", _("Просмотр сырого ответа KFF API")


class StaffActionLog(models.Model):
    """Единичная запись аудита. BigAutoField + без `updated_at` — та же
    логика, что и `analytics.models.AnalyticsEvent`: append-only таблица,
    запись неизменяема после создания."""

    id = models.BigAutoField(primary_key=True)
    created_at = models.DateTimeField(_("Когда"), auto_now_add=True, db_index=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="staff_action_logs", verbose_name=_("Кто"),
    )
    # Денормализованный снимок логина — переживает удаление/переименование
    # аккаунта, лог не должен становиться нечитаемым при офбординге сотрудника.
    actor_username = models.CharField(_("Логин (снимок)"), max_length=150, blank=True)
    action = models.CharField(_("Действие"), max_length=50, choices=AuditAction.choices, db_index=True)
    target = models.CharField(_("Объект действия"), max_length=300, blank=True)
    details = models.JSONField(_("Детали"), default=dict, blank=True)
    ip_address = models.GenericIPAddressField(_("IP"), null=True, blank=True)

    class Meta:
        verbose_name = _("Запись аудита staff")
        verbose_name_plural = _("Аудит-лог staff")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["action", "created_at"], name="staff_audit_action_time_idx"),
            models.Index(fields=["actor", "created_at"], name="staff_audit_actor_time_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.actor_username or 'system'} · {self.action} · {self.created_at:%Y-%m-%d %H:%M}"
