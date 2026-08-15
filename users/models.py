# users/models.py
"""
ИЗМЕНЕНИЯ В ЭТОМ ФАЙЛЕ (продуктовый аудит DOPX, часть 2):

1. `User` — добавлены `registration_ip`/`registration_user_agent` (антифрод:
   раньше IP/User-Agent сохранялись ТОЛЬКО для `ContactSubmission`, но не
   для регистрации — не с чем было сопоставлять кластеры аккаунтов с одного
   IP, см. `users/views.py::RegisterView`). Добавлен `email_digest_mode` в
   `DEFAULT_NOTIFICATION_SETTINGS` (см. `notifications/tasks.py`).
2. `UserBadge` — `BADGE_TYPES` теперь строится из единого каталога
   `users/badges.py` (было 8 достижений, стало 14 — полный список и
   обоснование каждого см. в `users/badges.py` и в продуктовом отчёте).
   `rarity`/`is_secret`/`description` — НЕ новые колонки БД, а properties
   поверх каталога (см. докстринг `users/badges.py`, зачем это важно для
   миграций).
3. `UserXP` — линейная кривая уровней (`level = total_xp // 100 + 1`,
   ровно 100 XP на любой уровень без изменений сложности) заменена на
   возрастающую (треугольную): каждый следующий уровень требует больше XP,
   чем предыдущий. `progress_percent` был математически некорректен для
   новой кривой (использовал `total_xp % 100`, что имеет смысл только при
   фиксированном шаге 100) — переписан через реальные границы текущего и
   следующего уровня.
4. Добавлена `SuspiciousActivityFlag` — раньше в проекте не было ВООБЩЕ
   никакого места, куда мог бы попасть сигнал "это похоже на накрутку" для
   ручной проверки: единственная реакция на подозрительное поведение была
   тихая корректировка `trust_score`. Теперь есть модель-очередь + Django
   admin поверх неё (см. `users/admin.py`).

Схема БД: пункты 1 и 4 требуют новых колонок/таблицы — после применения
файла обязательно выполнить `python manage.py makemigrations users`
локально против дев-БД (я не могу сгенерировать валидную миграцию без
доступа к реальному состоянию БД проекта — гадать содержимое файла миграции
вручную здесь было бы небезопасно, миграции слишком легко рассинхронизировать
руками). Пункт 2 (расширение `choices`) тоже потребует миграцию state-файла,
но не меняет структуру таблицы.
"""
from __future__ import annotations

import json
import math
import uuid

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from core.models import BaseModel
from users.badges import BADGE_CATALOG, BADGE_TYPE_CHOICES, RARITY_ORDER, BadgeDefinition

# Базовая "цена" уровня в XP. Кумулятивный порог для уровня N:
#   cumulative_xp_for_level(N) = LEVEL_XP_BASE * N * (N - 1)
# т.е. 2 уровень — 100 XP, 3 — 300, 4 — 600, 5 — 1000 и т.д. — растущий шаг.
LEVEL_XP_BASE = 50


def cumulative_xp_for_level(level: int) -> int:
    """Сколько суммарного XP нужно, чтобы ДОСТИЧЬ указанного уровня."""
    if level <= 1:
        return 0
    return LEVEL_XP_BASE * level * (level - 1)


def level_for_total_xp(total_xp: int) -> int:
    """
    Обратная функция к `cumulative_xp_for_level`: уровень по накопленному XP.

    Решает квадратное уравнение `LEVEL_XP_BASE * n * (n-1) <= total_xp`
    относительно `n` аналитически, а затем ПОДСТРАХОВЫВАЕТСЯ целочисленной
    коррекцией на случай погрешности плавающей точки ровно на границе
    уровня (`math.sqrt` от точного квадрата не всегда даёт ровно целое
    число из-за IEEE 754) — без коррекции пользователь мог бы на одной
    конкретной сумме XP видеть на 1 уровень меньше положенного.
    """
    if total_xp <= 0:
        return 1
    approx = (1 + math.sqrt(1 + 4 * total_xp / LEVEL_XP_BASE)) / 2
    level = max(1, int(approx))
    while cumulative_xp_for_level(level + 1) <= total_xp:
        level += 1
    while level > 1 and cumulative_xp_for_level(level) > total_xp:
        level -= 1
    return level


class User(AbstractUser, BaseModel):
    """Пользователь платформы."""

    email = models.EmailField(_("Email"), unique=True)
    avatar = models.ImageField(_("Аватар"), upload_to="avatars/", null=True, blank=True)
    bio = models.TextField(_("О себе"), blank=True)
    city = models.CharField(_("Город"), max_length=120, blank=True)
    rating_power = models.FloatField(_("Сила рейтинга"), default=1.0)
    trust_score = models.FloatField(_("Оценка доверия"), default=1.0)
    is_verified = models.BooleanField(_("Верифицирован"), default=False)
    # НОВОЕ: раньше публичный профиль (`/u/<username>/`, см.
    # users/views.py::PublicProfileView) был доступен для ЛЮБОГО активного
    # пользователя без возможности отказаться — единственная приватная
    # информация (email, IP, история сессий) и так не показывалась в
    # шаблоне, но сам факт "любой может открыть ваш профиль по нику" не
    # был опциональным. default=True — не ломает уже расшаренные ссылки на
    # существующие профили (см. users/leaderboard.html), выключить можно
    # из настроек профиля.
    is_profile_public = models.BooleanField(
        _("Публичный профиль"), default=True,
        help_text=_("Если выключено — /u/<username>/ отдаёт 404 для всех, кроме вас самих"),
    )
    verification_token = models.UUIDField(
        _("Токен верификации"), default=uuid.uuid4, editable=False, null=True, blank=True
    )
    verification_token_created_at = models.DateTimeField(
        _("Дата создания токена"), auto_now_add=True
    )

    # НОВОЕ: антифрод — см. пункт 1 в докстринге модуля.
    registration_ip = models.GenericIPAddressField(
        _("IP при регистрации"), null=True, blank=True
    )
    registration_user_agent = models.TextField(_("User-Agent при регистрации"), blank=True)

    _notification_settings = models.JSONField(
        _("Настройки уведомлений"), default=dict, blank=True, db_column="notification_settings"
    )

    total_evaluations = models.IntegerField(_("Всего оценок"), default=0)
    evaluation_streak = models.IntegerField(_("Серия оценок"), default=0)
    last_evaluation_date = models.DateField(_("Последняя оценка"), null=True, blank=True)

    DEFAULT_NOTIFICATION_SETTINGS = {
        "email_match_finished": True,
        "email_voting_closing": True,
        "email_new_badge": True,
        "email_level_up": True,
        "email_system": True,
        # НОВОЕ: см. notifications/tasks.py::send_notification_digest.
        # Если True — badge/level_up/trust-письма собираются в периодический
        # дайджест вместо мгновенной отправки на каждое событие.
        "email_digest_mode": True,
    }

    @property
    def notification_settings(self) -> dict:
        raw = self._notification_settings or {}
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (TypeError, ValueError):
                raw = {}
        if not isinstance(raw, dict):
            raw = {}
        return {**self.DEFAULT_NOTIFICATION_SETTINGS, **raw}

    @notification_settings.setter
    def notification_settings(self, value: dict) -> None:
        self._notification_settings = value

    def get_notification_setting(self, key: str, default: bool | None = None) -> bool:
        """Безопасное получение настройки уведомления."""
        return self.notification_settings.get(
            key, default if default is not None else self.DEFAULT_NOTIFICATION_SETTINGS.get(key, False)
        )

    def update_evaluation_stats(self) -> None:
        """Обновляет статистику оценок. Достижения проверяются отдельно."""
        today = timezone.now().date()
        self.total_evaluations += 1
        if self.last_evaluation_date:
            days_diff = (today - self.last_evaluation_date).days
            if days_diff == 1:
                self.evaluation_streak += 1
            elif days_diff > 1:
                self.evaluation_streak = 1
            # days_diff == 0 (та же дата) — серию не меняем.
        else:
            self.evaluation_streak = 1
        self.last_evaluation_date = today
        self.save(update_fields=["total_evaluations", "evaluation_streak", "last_evaluation_date", "updated_at"])

    def get_trust_level(self) -> tuple[str, str]:
        if self.trust_score >= 1.8:
            return "expert", _("Эксперт")
        if self.trust_score >= 1.4:
            return "reliable", _("Надёжный")
        if self.trust_score >= 1.0:
            return "standard", _("Стандартный")
        return "new", _("Новичок")

    def xp_multiplier(self) -> float:
        """
        Множитель начисляемого XP от `trust_score`, диапазон 0.8..1.2.

        Прямая связь "чем точнее ваши оценки, тем быстрее растёт уровень" —
        и дополнительный мягкий анти-фрод стимул: аккаунт с низким
        trust_score (подозрение в накрутке/невнимательности) прокачивается
        медленнее, даже если продолжает активно голосовать.
        `trust_score` в проекте всегда в диапазоне [0.5, 2.0] (см. clamp в
        `evaluations/views.py`), но на всякий случай клампим и здесь.
        """
        clamped = min(max(self.trust_score, 0.5), 2.0)
        return round(0.8 + (clamped - 0.5) / 1.5 * 0.4, 3)

    @property
    def unread_notifications_count(self) -> int:
        return self.notifications.filter(is_read=False).count()

    class Meta:
        verbose_name = _("Пользователь")
        verbose_name_plural = _("Пользователи")
        ordering = ["-trust_score", "-total_evaluations"]

    def __str__(self) -> str:
        return self.username


class UserBadge(BaseModel):
    """Достижения пользователей. Каталог — в `users/badges.py`."""

    BADGE_TYPES = BADGE_TYPE_CHOICES

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="badges", verbose_name=_("Пользователь"))
    badge_type = models.CharField(_("Тип достижения"), max_length=50, choices=BADGE_TYPES)
    awarded_at = models.DateTimeField(_("Дата получения"), auto_now_add=True)

    class Meta:
        verbose_name = _("Достижение")
        verbose_name_plural = _("Достижения")
        constraints = [models.UniqueConstraint(fields=["user", "badge_type"], name="unique_user_badge")]
        ordering = ["-awarded_at"]

    def __str__(self) -> str:
        return f"{self.user} - {self.definition.name if self.definition else self.badge_type}"

    @property
    def definition(self) -> BadgeDefinition | None:
        return BADGE_CATALOG.get(self.badge_type)

    @property
    def rarity(self) -> str:
        d = self.definition
        return d.rarity if d else "bronze"

    @property
    def rarity_order(self) -> int:
        return RARITY_ORDER.get(self.rarity, 0)

    @property
    def is_secret(self) -> bool:
        d = self.definition
        return d.is_secret if d else False

    @property
    def description(self) -> str:
        d = self.definition
        return d.description if d else ""

    def get_badge_type_display(self) -> str:  # noqa: D401 — совместимость с шаблонами/старым кодом
        d = self.definition
        return d.name if d else self.badge_type


class UserXP(BaseModel):
    """Опыт и рейтинг пользователя. Кривая уровней — см. `level_for_total_xp` выше."""

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="xp", verbose_name=_("Пользователь"))
    total_xp = models.IntegerField(_("Всего опыта"), default=0)
    level = models.IntegerField(_("Уровень"), default=1)

    class Meta:
        verbose_name = _("Опыт пользователя")
        verbose_name_plural = _("Опыт пользователей")

    def add_xp(self, amount: int) -> dict:
        """
        Начисляет XP и пересчитывает уровень по возрастающей кривой.

        :param amount: количество XP к начислению (может быть уже умножено
            на `User.xp_multiplier()` вызывающим кодом — см.
            `evaluations/views.py`).
        """
        old_level = self.level
        old_total_xp = self.total_xp

        self.total_xp = max(0, self.total_xp + int(round(amount)))
        new_level = level_for_total_xp(self.total_xp)

        levels_gained = list(range(old_level + 1, new_level + 1)) if new_level > old_level else []
        self.level = new_level

        self.save(update_fields=["level", "total_xp", "updated_at"])

        return {
            "level_increased": bool(levels_gained),
            "levels_gained": levels_gained,
            "old_level": old_level,
            "new_level": self.level,
            "old_total_xp": old_total_xp,
            "new_total_xp": self.total_xp,
            "xp_added": amount,
        }

    @property
    def xp_for_current_level(self) -> int:
        return cumulative_xp_for_level(self.level)

    @property
    def xp_for_next_level(self) -> int:
        return cumulative_xp_for_level(self.level + 1)

    @property
    def progress_percent(self) -> int:
        """
        Прогресс внутри ТЕКУЩЕГО уровня, 0..100.

        ИСПРАВЛЕНО: раньше `(total_xp % 100) / 100 * 100` — корректно только
        для фиксированного шага 100 XP/уровень. При возрастающей кривой это
        давало бессмысленные числа (например, на уровне 5, где шаг — 400 XP,
        `% 100` резало прогресс на 4 ложных "под-уровня" внутри одного
        настоящего). Теперь считается от реальных границ текущего/следующего
        уровня.
        """
        span = self.xp_for_next_level - self.xp_for_current_level
        if span <= 0:
            return 100
        return min(100, max(0, int(((self.total_xp - self.xp_for_current_level) / span) * 100)))

    def __str__(self) -> str:
        return f"{self.user} — Уровень {self.level} ({self.total_xp} XP)"


class SuspiciousActivityFlag(BaseModel):
    """
    Очередь сигналов возможной накрутки/бот-активности для ручной модерации.

    НОВАЯ МОДЕЛЬ (продуктовый аудит, раздел 4 «Анти-фрод»). Раньше в проекте
    не было НИ ОДНОГО места, куда стекались бы подозрительные сигналы —
    единственная реакция была тихая корректировка `trust_score` постфактум.
    Здесь — по конкретному источнику сигнала (скорость заполнения вайзарда,
    кластер аккаунтов с одного IP и т.д., дальше можно добавлять источники),
    непрерывный `score` и статус ручного разбора.
    """

    SOURCE_CHOICES = [
        ("fast_wizard", _("Слишком быстрое заполнение вайзарда оценки")),
        ("ip_cluster", _("Кластер аккаунтов с одного IP")),
        ("extreme_bias", _("Экстремальная историческая предвзятость")),
        ("manual", _("Отмечено вручную модератором")),
    ]
    STATUS_CHOICES = [
        ("pending", _("Ожидает проверки")),
        ("confirmed", _("Подтверждено — накрутка")),
        ("dismissed", _("Отклонено — ложное срабатывание")),
    ]

    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="suspicious_activity_flags", verbose_name=_("Пользователь")
    )
    match = models.ForeignKey(
        "matches.Match",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="suspicious_activity_flags",
        verbose_name=_("Матч"),
    )
    source = models.CharField(_("Источник сигнала"), max_length=30, choices=SOURCE_CHOICES)
    score = models.FloatField(_("Скор подозрительности"), default=0.0, help_text=_("0.0 (незначительно) .. 1.0 (крайне подозрительно)"))
    details = models.JSONField(_("Детали"), default=dict, blank=True)
    status = models.CharField(_("Статус"), max_length=20, choices=STATUS_CHOICES, default="pending")
    reviewed_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewed_suspicious_flags",
        verbose_name=_("Проверил"),
    )
    reviewed_at = models.DateTimeField(_("Дата проверки"), null=True, blank=True)

    class Meta:
        verbose_name = _("Сигнал подозрительной активности")
        verbose_name_plural = _("Сигналы подозрительной активности")
        indexes = [
            models.Index(fields=["status", "-created_at"]),
            models.Index(fields=["user", "-created_at"]),
        ]
        ordering = ["-score", "-created_at"]

    def __str__(self) -> str:
        return f"{self.get_source_display()} — {self.user} ({self.score:.2f})"


class Follow(BaseModel):
    """
    Продуктовый аудит, раздел 5b ("Follow-граф"): подписка пользователя на
    игрока ИЛИ команду. Одна модель с двумя nullable FK (а не GenericForeignKey
    или две отдельные модели PlayerFollow/TeamFollow) — компромисс,
    оправданный тем, что в проекте больше нигде нет GenericForeignKey (не
    хотим вводить новый паттерн ради одной фичи), а единый queryset
    `user.follows.all()` нужен для страницы "На кого вы подписаны" без
    UNION двух таблиц. Ровно одно из полей заполнено — гарантируется
    `CheckConstraint` на уровне БД, а не только валидацией в форме/view,
    чтобы прямая запись в БД (миграции данных, консоль) не могла создать
    "подписку в никуда".
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='follows', verbose_name=_('Пользователь')
    )
    player = models.ForeignKey(
        'players.Player', on_delete=models.CASCADE, null=True, blank=True,
        related_name='followers', verbose_name=_('Игрок'),
    )
    team = models.ForeignKey(
        'teams.Team', on_delete=models.CASCADE, null=True, blank=True,
        related_name='followers', verbose_name=_('Команда'),
    )

    class Meta:
        verbose_name = _('Подписка')
        verbose_name_plural = _('Подписки')
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'player'], name='unique_follow_player',
                condition=models.Q(player__isnull=False),
            ),
            models.UniqueConstraint(
                fields=['user', 'team'], name='unique_follow_team',
                condition=models.Q(team__isnull=False),
            ),
            models.CheckConstraint(
                # `condition=`, не `check=` — kwarg `check` был удалён в
                # Django 6.0 (deprecated с 5.1), этот проект на Django 6.0.3
                # (см. заголовки существующих миграций "Generated by Django
                # 6.0.3").
                condition=(
                    models.Q(player__isnull=False, team__isnull=True)
                    | models.Q(player__isnull=True, team__isnull=False)
                ),
                name='follow_exactly_one_target',
            ),
        ]
        indexes = [
            # Явное имя — та же причина, что у event_reaction_type_idx в
            # events/models.py: миграции пишутся вручную без доступа к
            # реальной БД, автогенерируемый Django-хеш здесь непредсказуем.
            models.Index(fields=['user'], name='follow_user_idx'),
        ]

    def __str__(self) -> str:
        target = self.player or self.team
        return f"{self.user.username} → {target}"


class PushSubscription(BaseModel):
    """
    Продуктовый аудит, раздел 5c ("PWA + Web Push"): подписка браузера
    пользователя на Web Push (Push API). `endpoint` — уникальный URL,
    выданный push-сервисом браузера (FCM для Chrome, Mozilla push service
    для Firefox и т.д.) — это и есть "адрес", на который сервер шлёт push
    через `pywebpush` (см. `notifications/services.py::send_push_to_user`).

    Один пользователь может иметь НЕСКОЛЬКО подписок одновременно (телефон
    + ноутбук + другой браузер) — поэтому `user` НЕ уникален сам по себе,
    уникален `endpoint` (одна и та же связка браузер+устройство физически
    не может быть подписана дважды).
    """
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='push_subscriptions',
        verbose_name=_('Пользователь'),
    )
    endpoint = models.URLField(_('Endpoint'), max_length=500, unique=True)
    p256dh = models.CharField(_('Ключ p256dh'), max_length=255)
    auth = models.CharField(_('Ключ auth'), max_length=255)
    user_agent = models.CharField(_('User-Agent'), max_length=255, blank=True)

    class Meta:
        verbose_name = _('Push-подписка')
        verbose_name_plural = _('Push-подписки')
        indexes = [
            models.Index(fields=['user'], name='push_subscription_user_idx'),
        ]

    def __str__(self) -> str:
        return f"{self.user.username} — {self.endpoint[:40]}..."