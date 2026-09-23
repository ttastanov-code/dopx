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

import uuid

from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
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
    CELERY_TASK_REVOKED = "celery_task_revoked", _("Отзыв/остановка celery-задачи")
    SPORTMONKS_HEALTH_CHECK = "sportmonks_health_check", _("Проверка доступности Sportmonks API")
    SYSTEM_ANNOUNCEMENT_SENT = "system_announcement_sent", _("Отправлено системное объявление")
    # НОВОЕ (2026-09-09, Центр доверия к данным) — dashboard/views.py::
    # data_trust, dashboard/views.py::data_trust_report_action,
    # dashboard/views.py::data_trust_discrepancy_review.
    DATA_ERROR_REPORT_RESOLVED = "data_error_report_resolved", _("Жалоба на данные матча закрыта")
    PARSER_DISCREPANCY_REVIEWED = "parser_discrepancy_reviewed", _("Расхождение импорта разобрано")
    # НОВОЕ (2026-09-22, прямая просьба пользователя: "seed_full_history
    # надо вывести в дашборд... все наши тестовые скрипты и команды в
    # отдельный раздел") — см. dashboard/commands_registry.py,
    # dashboard/command_runner.py, ManagementCommandRun ниже.
    MANAGEMENT_COMMAND_TRIGGERED = "management_command_triggered", _("Запуск management-команды из дашборда")
    # НОВОЕ (2026-09-22, очередь «Проверка ФИО (ИИ)») — см. parsers/models.py::
    # NameVerificationSuggestion, dashboard/views.py::names_review_action.
    NAME_SUGGESTION_APPROVED = "name_suggestion_approved", _("Предложение ИИ по ФИО подтверждено")
    NAME_SUGGESTION_REJECTED = "name_suggestion_rejected", _("Предложение ИИ по ФИО отклонено")
    # НОВОЕ (2026-09-22, очередь «Дубли игроков») — см. players/models.py::
    # PotentialDuplicatePlayer, players/services.py::merge_players,
    # dashboard/views.py::duplicate_players_merge/duplicate_players_dismiss.
    DUPLICATE_PLAYERS_MERGED = "duplicate_players_merged", _("Дубли игроков объединены")
    DUPLICATE_PLAYER_FLAG_DISMISSED = "duplicate_player_flag_dismissed", _("Флаг дубля игрока отклонён (разные люди)")
    # НОВОЕ (2026-09-23, раздел «Матчи») — см. dashboard/views.py::match_detail/
    # match_trigger_recalc. MATCH_MANUAL_EDIT — правка полей матча (статус/
    # счёт/дата/тур) отдельно от MATCH_RESYNC (тот — full re-fetch с
    # Sportmonks, этот — staff вписал значения руками).
    MATCH_MANUAL_EDIT = "match_manual_edit", _("Матч отредактирован вручную")
    MATCH_RECALC_TRIGGERED = "match_recalc_triggered", _("Ручной пересчёт агрегатов матча")
    # НОВОЕ (2026-09-23, раздел «Настройки платформы») — см. core/models.py::
    # PlatformSetting, dashboard/views.py::platform_settings*.
    PLATFORM_SETTING_CREATED = "platform_setting_created", _("Создана настройка платформы")
    PLATFORM_SETTING_CHANGED = "platform_setting_changed", _("Изменена настройка платформы")
    PLATFORM_SETTING_DELETED = "platform_setting_deleted", _("Удалена настройка платформы")
    # НОВОЕ (2026-09-23, раздел «Пользователи») — см. dashboard/views.py::
    # user_detail/user_toggle_ban/user_reset_trust_score.
    USER_BANNED = "user_banned", _("Пользователь заблокирован")
    USER_UNBANNED = "user_unbanned", _("Пользователь разблокирован")
    USER_TRUST_SCORE_RESET = "user_trust_score_reset", _("Оценка доверия пользователя сброшена")
    # НОВОЕ (2026-09-23, раздел «Модерация оценок») — см. evaluations/
    # models.py::EvaluationSession, dashboard/views.py::
    # evaluation_session_delete. Удаление сессии удаляет и все связанные
    # под-оценки (Context/Team/Player/Coach/Referee/MatchEvaluation) того
    # же (user, match) — фрод/спам-оценка выпиливается целиком, не по частям.
    EVALUATION_SESSION_DELETED = "evaluation_session_deleted", _("Сессия оценки удалена (модерация)")
    # НОВОЕ (2026-09-23, раздел «Партнёры и баннеры») — см. partners/
    # models.py::Partner/Banner, dashboard/views.py::partner_create/
    # partner_update/partner_delete/banner_create/banner_update/banner_delete.
    PARTNER_CREATED = "partner_created", _("Партнёр создан")
    PARTNER_UPDATED = "partner_updated", _("Партнёр изменён")
    PARTNER_DELETED = "partner_deleted", _("Партнёр удалён")
    BANNER_CREATED = "banner_created", _("Баннер создан")
    BANNER_UPDATED = "banner_updated", _("Баннер изменён")
    BANNER_DELETED = "banner_deleted", _("Баннер удалён")
    # НОВОЕ (2026-09-23, раздел «Роли доступа») — см. StaffAccessGrant ниже,
    # dashboard/views.py::access_roles_update.
    ACCESS_GRANT_UPDATED = "access_grant_updated", _("Права доступа сотрудника изменены")

    # 2026-09-09: RAW_KFF_LOOKUP/KFF_HEALTH_CHECK удалены вместе со всем
    # KFF-парсером (по решению пользователя). STADIUM_MARKED_REVIEWED
    # удалён в тот же день вместе со всей моделью Stadium (см.
    # matches/models.py, core/models_stadium.py — принципиальная проблема
    # с venue-данными КПЛ, не просто отдельные ошибки сопоставления). Уже
    # существующие строки StaffActionLog с этими значениями в БД не трогаем
    # и не удаляем — они просто перестают резолвиться в красивый label
    # через get_action_display() и будут показывать сырое значение
    # ("raw_kff_lookup"/"kff_health_check"/"stadium_marked_reviewed") в
    # истории аудита. Это осознанный компромисс: исторический лог
    # неизменяем (см. докстринг StaffActionLog), а не повод держать мёртвые
    # choices вечно.


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
    # НАЙДЕНО (2026-09-23, пользователь заметил ошибку в списке ошибок
    # дашборда): "Object of type datetime is not JSON serializable" — без
    # encoder здесь JSONField сериализует через обычный json.dumps, который
    # не умеет datetime/UUID/Decimal. dashboard/views.py::
    # parser_sportmonks_health_check передаёт в details весь результат
    # health-check'а как есть, а там результат содержит "checked_at" —
    # РЕАЛЬНЫЙ datetime-объект (нужен таким для фильтра |timesince в
    # шаблоне, не может быть строкой). Падение происходило именно на этом
    # поле. Систематический фикс — на самой модели, а не на одном call
    # site: у log_staff_action (dashboard/audit.py) ~25 мест вызова по
    # всему дашборду, каждое передаёт свой произвольный словарь details, и
    # в любом из них рано или поздно может оказаться datetime/UUID/Decimal
    # без ручного приведения к строке — DjangoJSONEncoder умеет все три
    # типа "из коробки", закрывает весь класс бага разом.
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


class ManagementCommandRun(models.Model):
    """Один запуск management-команды из раздела "Скрипты и команды"
    (dashboard/commands_registry.py — allowlist команд + их аргументов,
    dashboard/command_runner.py — сборка call_command()/запуск,
    dashboard/tasks.py::run_management_command — сам Celery-таск).

    Асинхронное выполнение (а не синхронный call_command() прямо во view) —
    принципиально: seed_full_history на пару туров истории может идти
    минуты, HTTP-запрос staff-панели не должен висеть всё это время (и
    упадёт по таймауту прокси/gunicorn раньше, чем команда реально
    закончит). Строка создаётся статусом PENDING синхронно (staff сразу
    видит её в истории), сам вызов исполняется воркером, а страница
    поллит статус через scripts_run_status_partial (тот же приём, что
    celery-задачи парсера — dashboard/_celery_tasks_card.html).

    `stdout`/`stderr` — реальный вывод call_command(..., stdout=StringIO(),
    stderr=StringIO()) — большинство наших команд печатают отчёт (сколько
    записей создано/удалено/пропущено) именно туда, это и есть "результат"
    для staff, не только факт success/failure."""

    class Status(models.TextChoices):
        PENDING = "pending", _("В очереди")
        RUNNING = "running", _("Выполняется")
        SUCCESS = "success", _("Успешно")
        FAILED = "failed", _("Ошибка")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    command_name = models.CharField(_("Команда"), max_length=100, db_index=True)
    # Аргументы, с которыми запущена команда — снимок того, что реально
    # ушло в call_command() (уже провалидированное/приведённое к типам
    # dashboard/commands_registry.py, а не сырой request.POST).
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

    class Meta:
        verbose_name = _("Запуск management-команды")
        verbose_name_plural = _("Запуски management-команд")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["command_name", "created_at"], name="cmd_run_name_time_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.command_name} · {self.get_status_display()} · {self.created_at:%Y-%m-%d %H:%M}"


# =============================================================================
# 2026-09-23, раздел «Роли доступа» — прямая просьба пользователя заменить
# единственный переключатель is_staff ("всё или ничего") на гибкие права по
# разделам дашборда: сотрудник может модерировать пользователей, но не
# видеть Скрипты/Настройки платформы, например. DASHBOARD_SECTIONS — единый
# каталог разделов (тот же принцип, что AuditAction выше — ключи здесь
# используются И в проверке доступа (dashboard/access.py), И в шаблоне
# «Роли доступа», И в _nav.html при решении, какие вкладки показывать).
#
# Сознательно БЕЗ builtin Django permissions/Groups — тем нужен отдельный
# Permission-объект на каждый раздел + Group-модель + UI их назначения,
# избыточно для ~17 плоских разделов одного дашборда. JSONField-список
# ключей — простой, читаемый в БД без джойнов, и правится тем же паттерном
# редактирования, что PlatformSetting/остальные новые разделы этой сессии.
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
    ("parser_tools", _("Парсер")),
    ("ads", _("Реклама (+ партнёры/баннеры)")),
    ("audit", _("Аудит")),
    ("announcements", _("Объявления")),
    ("platform_settings", _("Настройки платформы")),
    ("system_status", _("Системный статус")),
    ("scripts", _("Скрипты и команды")),
]
DASHBOARD_SECTION_KEYS = [key for key, _label in DASHBOARD_SECTIONS]


class StaffAccessGrant(models.Model):
    """Список разделов дашборда, доступных КОНКРЕТНОМУ staff-пользователю.

    ВАЖНО, безопасный дефолт (dashboard/access.py::user_can_access_section):
    - is_superuser=True — ВСЕГДА полный доступ, эта модель на них не
      действует вообще (нельзя случайно закрыть себе или другому
      суперпользователю весь дашборд через форму).
    - Staff БЕЗ записи StaffAccessGrant — тоже полный доступ (grandfather-
      правило: раньше единственным гейтом был is_staff, у всех уже
      работающих сотрудников был доступ ко всему; включение этой модели не
      должно НИКОГО молча отрезать от разделов, которыми он уже пользуется).
      Ограничение начинается ТОЛЬКО когда админ явно создал для человека
      запись и снял чекбоксы конкретных разделов — opt-in restriction, а не
      opt-out.
    - allowed_sections=[] на СУЩЕСТВУЮЩЕЙ записи — осознанно означает "нет
      доступа никуда" (админ явно сохранил пустой список), это НЕ то же
      самое, что отсутствие записи выше.
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
