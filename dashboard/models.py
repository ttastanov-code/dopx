# dashboard/models.py
"""Модели staff-дашборда: аудит-лог, запуски команд, роли доступа.
CRUD через ModelAdmin и так пишется в django_admin_log;
StaffActionLog — для кастомных действий дашборда (dashboard/audit.py::log_staff_action).
"""
from __future__ import annotations

import uuid

from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.utils.translation import gettext_lazy as _


class AuditAction(models.TextChoices):
    """Каталог действий для аудита. Писать action только через этот Enum."""

    ANTIFRAUD_FLAG_CONFIRMED = "antifraud_flag_confirmed", _("Флаг подтверждён")
    ANTIFRAUD_FLAG_DISMISSED = "antifraud_flag_dismissed", _("Флаг отклонён")
    MATCH_RESYNC = "match_resync", _("Ручной ресинк матча")
    CELERY_TASK_TRIGGERED = "celery_task_triggered", _("Запуск celery-задачи вручную")
    CELERY_TASK_REVOKED = "celery_task_revoked", _("Отзыв/остановка celery-задачи")
    SERVICE_RESTART = "service_restart", _("Перезапуск сервиса (бот, воркер)")
    SPORTMONKS_HEALTH_CHECK = "sportmonks_health_check", _("Проверка доступности Sportmonks API")
    SYSTEM_ANNOUNCEMENT_SENT = "system_announcement_sent", _("Отправлено системное объявление")
    MOURNING_CHANGED = "mourning_changed", _("Изменён режим траура")
    # Центр доверия к данным.
    DATA_ERROR_REPORT_RESOLVED = "data_error_report_resolved", _("Жалоба на данные матча закрыта")
    PARSER_DISCREPANCY_REVIEWED = "parser_discrepancy_reviewed", _("Расхождение импорта разобрано")
    # Скрипты и команды.
    MANAGEMENT_COMMAND_TRIGGERED = "management_command_triggered", _("Запуск management-команды из дашборда")
    # Проверка ФИО (ИИ).
    NAME_SUGGESTION_APPROVED = "name_suggestion_approved", _("Предложение ИИ по ФИО подтверждено")
    NAME_SUGGESTION_REJECTED = "name_suggestion_rejected", _("Предложение ИИ по ФИО отклонено")
    # Дубли игроков.
    DUPLICATE_PLAYERS_MERGED = "duplicate_players_merged", _("Дубли игроков объединены")
    DUPLICATE_PLAYER_FLAG_DISMISSED = "duplicate_player_flag_dismissed", _("Флаг дубля игрока отклонён (разные люди)")
    # Раздел «Матчи». MATCH_MANUAL_EDIT — ручная правка, MATCH_RESYNC — перезагрузка с Sportmonks.
    MATCH_MANUAL_EDIT = "match_manual_edit", _("Матч отредактирован вручную")
    MATCH_RECALC_TRIGGERED = "match_recalc_triggered", _("Ручной пересчёт агрегатов матча")
    # Настройки платформы.
    PLATFORM_SETTING_CREATED = "platform_setting_created", _("Создана настройка платформы")
    PLATFORM_SETTING_CHANGED = "platform_setting_changed", _("Изменена настройка платформы")
    PLATFORM_SETTING_DELETED = "platform_setting_deleted", _("Удалена настройка платформы")
    # Пользователи.
    USER_BANNED = "user_banned", _("Пользователь заблокирован")
    USER_UNBANNED = "user_unbanned", _("Пользователь разблокирован")
    USER_TRUST_SCORE_RESET = "user_trust_score_reset", _("Оценка доверия пользователя сброшена")
    USER_REPORT_HANDLED = "user_report_handled", _("Жалоба пользователя разобрана")
    # Модерация оценок. Удаление сессии удаляет и все её под-оценки.
    EVALUATION_SESSION_DELETED = "evaluation_session_deleted", _("Сессия оценки удалена (модерация)")
    # Партнёры и баннеры.
    PARTNER_CREATED = "partner_created", _("Партнёр создан")
    PARTNER_UPDATED = "partner_updated", _("Партнёр изменён")
    PARTNER_DELETED = "partner_deleted", _("Партнёр удалён")
    BANNER_CREATED = "banner_created", _("Баннер создан")
    BANNER_UPDATED = "banner_updated", _("Баннер изменён")
    BANNER_DELETED = "banner_deleted", _("Баннер удалён")
    EXPERT_TAKE_SAVED = "expert_take_saved", _("Мнение эксперта сохранено")
    EXPERT_TAKE_DELETED = "expert_take_deleted", _("Мнение эксперта удалено")
    EXPERT_SAVED = "expert_saved", _("Эксперт сохранён")
    EXPERT_INVITE_CREATED = "expert_invite_created", _("Создана ссылка для эксперта")
    EXPERT_INVITE_CHANGED = "expert_invite_changed", _("Ссылка для эксперта изменена")
    BOT_ACTION = "bot_action", _("Действие через Telegram-бот")
    CHANNEL_POST = "channel_post", _("Пост в Telegram-канал")
    # Роли доступа.
    ACCESS_GRANT_UPDATED = "access_grant_updated", _("Права доступа сотрудника изменены")
    # Вкл/выкл синка Sportmonks.
    SPORTMONKS_SYNC_TOGGLED = "sportmonks_sync_toggled", _("Синк с Sportmonks включён/выключен")

    # Старые значения (KFF, стадионы) удалены из choices; строки в логе остаются как есть.


class StaffActionLog(models.Model):
    """Запись аудита. Append-only, после создания не меняется."""

    id = models.BigAutoField(primary_key=True)
    created_at = models.DateTimeField(_("Когда"), auto_now_add=True, db_index=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="staff_action_logs", verbose_name=_("Кто"),
    )
    # Снимок логина — лог читаем и после удаления аккаунта.
    actor_username = models.CharField(_("Логин (снимок)"), max_length=150, blank=True)
    action = models.CharField(_("Действие"), max_length=50, choices=AuditAction.choices, db_index=True)
    target = models.CharField(_("Объект действия"), max_length=300, blank=True)
    # DjangoJSONEncoder — в details бывают datetime/UUID/Decimal.
    details = models.JSONField(_("Детали"), default=dict, blank=True, encoder=DjangoJSONEncoder)
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


# Пометка в stderr при ручной остановке — такой запуск не считается упавшим.
MANUAL_STOP_NOTE = "Остановлено вручную staff (terminate)."
# Запуск без признаков жизни: воркер перезапустили или он упал посреди команды.
INTERRUPTED_NOTE = "Прервано: воркер перезапустился или упал. Запустите ещё раз — команда продолжит с того места."
# Пока команда идёт, alive_at обновляется раз в ALIVE_EVERY; тишина дольше ALIVE_TIMEOUT — запуск оборван.
ALIVE_EVERY_SECONDS = 20
ALIVE_TIMEOUT_SECONDS = 180


class ManagementCommandRun(models.Model):
    """Запуск management-команды из «Скриптов и команд».
    Выполняется в Celery (dashboard/tasks.py::run_management_command), страница поллит статус.
    stdout/stderr — вывод команды.
    """

    class Status(models.TextChoices):
        PENDING = "pending", _("В очереди")
        RUNNING = "running", _("Выполняется")
        SUCCESS = "success", _("Успешно")
        FAILED = "failed", _("Ошибка")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    command_name = models.CharField(_("Команда"), max_length=100, db_index=True)
    # Аргументы после валидации в commands_registry.
    args = models.JSONField(_("Аргументы"), default=dict, blank=True)
    status = models.CharField(_("Статус"), max_length=10, choices=Status.choices, default=Status.PENDING, db_index=True)
    stdout = models.TextField(_("Вывод"), blank=True)
    stderr = models.TextField(_("Ошибки"), blank=True)
    triggered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="management_command_runs", verbose_name=_("Кто запустил"),
    )
    triggered_by_username = models.CharField(_("Логин (снимок)"), max_length=150, blank=True)
    created_at = models.DateTimeField(_("Поставлена"), auto_now_add=True, db_index=True)
    started_at = models.DateTimeField(_("Начата"), null=True, blank=True)
    finished_at = models.DateTimeField(_("Завершена"), null=True, blank=True)
    celery_task_id = models.CharField(_("ID celery-задачи"), max_length=255, blank=True)
    alive_at = models.DateTimeField(_("Последний признак жизни"), null=True, blank=True)

    class Meta:
        verbose_name = _("Запуск management-команды")
        verbose_name_plural = _("Запуски management-команд")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["command_name", "created_at"], name="cmd_run_name_time_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.command_name} · {self.get_status_display()} · {self.created_at:%Y-%m-%d %H:%M}"

    @classmethod
    def mark_interrupted(cls) -> int:
        """«Выполняется» без признаков жизни дольше ALIVE_TIMEOUT -> «Ошибка» с пометкой «Прервано»."""
        from datetime import timedelta

        from django.db.models import Q
        from django.utils import timezone

        cutoff = timezone.now() - timedelta(seconds=ALIVE_TIMEOUT_SECONDS)
        stale = cls.objects.filter(status=cls.Status.RUNNING).filter(
            Q(alive_at__lt=cutoff) | Q(alive_at__isnull=True, started_at__lt=cutoff))
        n = 0
        for run in stale:
            run.status = cls.Status.FAILED
            run.stderr = (run.stderr + "\n" if run.stderr else "") + INTERRUPTED_NOTE
            run.finished_at = timezone.now()
            run.save(update_fields=["status", "stderr", "finished_at"])
            n += 1
        return n


# =============================================================================
# Роли доступа: права staff по разделам дашборда.
# DASHBOARD_SECTIONS используется в dashboard/access.py, шаблоне ролей и _nav.html.
# =============================================================================

DASHBOARD_SECTIONS = [
    ("overview", _("Обзор")),
    ("traffic", _("Трафик")),
    ("matches", _("Матчи")),
    ("data_health", _("Здоровье данных")),
    ("data_trust", _("Доверие к данным")),
    ("duplicate_players", _("Дубли игроков")),
    ("names_review", _("Проверка ФИО")),
    ("evaluation_sessions", _("Модерация оценок")),
    ("users", _("Пользователи")),
    ("antifraud", _("Антифрод")),
    ("reports", _("Жалобы пользователей")),
    ("parser_tools", _("Парсер")),
    ("ads", _("Реклама (+ партнёры/баннеры)")),
    ("audit", _("Аудит")),
    ("announcements", _("Объявления")),
    ("mourning", _("Режим траура")),
    ("social_content", _("Контент для соцсетей")),
    ("experts", _("Мнения экспертов")),
    ("channel", _("Telegram-канал")),
    ("admin_bot", _("Telegram-бот")),
    ("platform_settings", _("Настройки платформы")),
    ("system_status", _("Системный статус")),
    ("scripts", _("Скрипты и команды")),
]
DASHBOARD_SECTION_KEYS = [key for key, _label in DASHBOARD_SECTIONS]


class StaffAccessGrant(models.Model):
    """Разделы дашборда, доступные staff-пользователю.
    - суперпользователь — всегда полный доступ;
    - нет записи или allowed_sections=[] — доступа нет никуда.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="dashboard_access_grant", verbose_name=_("Пользователь"),
    )
    allowed_sections = models.JSONField(
        _("Разрешённые разделы"), default=list, blank=True,
        help_text=_("Ключи из DASHBOARD_SECTIONS — какие вкладки дашборда видит и может открыть этот сотрудник"),
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name=_("Кто настраивал"),
    )
    updated_at = models.DateTimeField(_("Изменено"), auto_now=True)

    class Meta:
        verbose_name = _("Права доступа сотрудника")
        verbose_name_plural = _("Права доступа сотрудников")

    def __str__(self) -> str:
        return f"{self.user.username}: {len(self.allowed_sections)} разделов"

    def has_section(self, section_key: str) -> bool:
        return section_key in self.allowed_sections


class StaffRole(models.Model):
    """Роль сотрудника: группа Django + уровни доступа по разделам (dashboard/roles.py).
    Права группы на модели выставляются из уровней автоматически."""

    group = models.OneToOneField("auth.Group", on_delete=models.CASCADE, related_name="staff_role", verbose_name=_("Группа"))
    description = models.CharField(_("Описание"), max_length=200, blank=True)
    levels = models.JSONField(_("Уровни по разделам"), default=dict, blank=True)
    updated_at = models.DateTimeField(_("Изменено"), auto_now=True)

    class Meta:
        verbose_name = _("Роль сотрудника")
        verbose_name_plural = _("Роли сотрудников")

    def __str__(self):
        return self.group.name
