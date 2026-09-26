# users/models.py
"""Модели пользователей: User, достижения, опыт, подписки, push-подписки,
антифрод-флаги и калибруемые пороги.
"""
from __future__ import annotations

import json
import math
import uuid

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.contrib.contenttypes.fields import GenericForeignKey
from django.db import models, transaction
from django.utils.translation import gettext_lazy as _

from core.models import BaseModel
from users.badges import BADGE_CATALOG, BADGE_TYPE_CHOICES, RARITY_ORDER, BadgeDefinition
from users.kz_cities import KZ_CITY_CHOICES

# Кумулятивный порог уровня N: LEVEL_XP_BASE * N * (N - 1)
# (2 уровень — 100 XP, 3 — 300, 4 — 600, ...).
LEVEL_XP_BASE = 50


def cumulative_xp_for_level(level: int) -> int:
    """Сколько суммарного XP нужно для уровня."""
    if level <= 1:
        return 0
    return LEVEL_XP_BASE * level * (level - 1)


def level_for_total_xp(total_xp: int) -> int:
    """Уровень по накопленному XP. Аналитическое решение + целочисленная
    коррекция от ошибок float на границе уровня.
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


def is_email_verified(user) -> bool:
    """Подтверждена ли почта — из БД: OTPMiddleware подменяет request.user.is_verified функцией."""
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    return User.objects.filter(pk=user.pk, is_verified=True).exists()


class User(AbstractUser, BaseModel):
    """Пользователь платформы."""

    email = models.EmailField(_("Email"), unique=True)
    # Нормализованный email (core.utils.canonical_email) — поиск дублей ящика.
    email_canonical = models.CharField(_("Email (нормализованный)"), max_length=254, blank=True, db_index=True, editable=False)
    avatar = models.ImageField(_("Аватар"), upload_to="avatars/", null=True, blank=True)
    bio = models.TextField(_("О себе"), blank=True)
    # Город — из справочника users/kz_cities.py. blank=True оставлен ради старых
    # записей; обязателен только в форме регистрации.
    city = models.CharField(_("Город"), max_length=120, blank=True, choices=KZ_CITY_CHOICES)
    rating_power = models.FloatField(_("Сила рейтинга"), default=1.0)
    trust_score = models.FloatField(_("Оценка доверия"), default=1.0)
    is_verified = models.BooleanField(_("Верифицирован"), default=False)
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

    # Для поиска кластеров аккаунтов с одного IP.
    registration_ip = models.GenericIPAddressField(
        _("IP при регистрации"), null=True, blank=True
    )
    registration_user_agent = models.TextField(_("User-Agent при регистрации"), blank=True)

    _notification_settings = models.JSONField(
        _("Настройки уведомлений"), default=dict, blank=True, db_column="notification_settings"
    )

    total_evaluations = models.IntegerField(_("Всего оценок"), default=0)

    # Серия оценок — по турам чемпионата. См. docs/adr/0022-streak-semantics-redesign.md.
    evaluation_streak = models.IntegerField(_("Серия оценок"), default=0)
    last_evaluation_season_id = models.UUIDField(
        _("Сезон последней оценки"), null=True, blank=True
    )
    last_evaluation_tour = models.PositiveSmallIntegerField(
        _("Тур последней оценки"), null=True, blank=True
    )

    # Серия угаданных исходов подряд (считается после матча).
    prediction_streak = models.IntegerField(_("Серия угаданных прогнозов"), default=0)

    DEFAULT_NOTIFICATION_SETTINGS = {
        "email_match_finished": True,
        "email_voting_closing": True,
        "email_new_badge": True,
        "email_level_up": True,
        "email_system": True,
        # True — письма о бейджах/уровнях/системные собираются в дайджест.
        "email_digest_mode": True,
        # Петли удержания:
        "email_prediction_closing": True,  # скоро закроется приём прогнозов
        "email_weekly_summary": True,  # сводка недели
        "email_prediction_result": True,  # результат прогноза
        # итоги «Лучшие тура»
        "email_round_results": True,
        # Push по типам; ключи — notifications.services.PUSH_KIND_SETTING.
        "push_live": True,
        "push_lineups": True,
        "push_voting": True,
        "push_predictions": True,
        "push_achievements": True,
        "push_round_results": True,
        "push_match_changes": True,
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

    def save(self, *args, **kwargs):
        from core.utils import canonical_email

        self.email_canonical = canonical_email(self.email)
        update_fields = kwargs.get("update_fields")
        if update_fields is not None and "email" in update_fields:
            kwargs["update_fields"] = {*update_fields, "email_canonical"}
        super().save(*args, **kwargs)

    def refresh_verification_token(self) -> None:
        """Новый токен подтверждения почты со свежим сроком жизни."""
        from django.utils import timezone

        self.verification_token = uuid.uuid4()
        self.verification_token_created_at = timezone.now()
        type(self).objects.filter(pk=self.pk).update(
            verification_token=self.verification_token,
            verification_token_created_at=self.verification_token_created_at,
        )

    def update_evaluation_stats(self, match) -> None:
        """Обновляет счётчик оценок и серию по турам.

        Если у матча нет тура — серию не трогаем. Оценка более раннего тура,
        чем уже засчитанный (пропущенный/перенесённый матч), серию не рвёт.
        """
        self.total_evaluations += 1
        tour = match.tour
        if tour is not None:
            same_season = self.last_evaluation_season_id == match.season_id
            if same_season and self.last_evaluation_tour == tour:
                pass  # тот же тур
            elif (
                same_season
                and self.last_evaluation_tour is not None
                and tour == self.last_evaluation_tour + 1
            ):
                self.evaluation_streak += 1  # следующий тур подряд
                self.last_evaluation_tour = tour
            elif (
                same_season
                and self.last_evaluation_tour is not None
                and tour < self.last_evaluation_tour
            ):
                # Тур раньше уже засчитанного максимума — серию не трогаем.
                pass
            else:
                self.evaluation_streak = 1  # разрыв, новый сезон или первая оценка
                self.last_evaluation_tour = tour
            self.last_evaluation_season_id = match.season_id
        self.save(update_fields=[
            "total_evaluations", "evaluation_streak",
            "last_evaluation_season_id", "last_evaluation_tour", "updated_at",
        ])

    def update_prediction_stats(self, is_correct: bool) -> None:
        """Серия угаданных прогнозов подряд. Вызывается из notify_prediction_results
        после завершения матча, по порядку end_time.
        """
        if is_correct:
            self.prediction_streak += 1
        else:
            self.prediction_streak = 0
        self.save(update_fields=["prediction_streak", "updated_at"])

    def get_trust_level(self) -> tuple[str, str]:
        if self.trust_score >= 1.8:
            return "expert", _("Эксперт")
        if self.trust_score >= 1.4:
            return "reliable", _("Надёжный")
        if self.trust_score >= 1.0:
            return "standard", _("Стандартный")
        return "new", _("Новичок")

    def xp_multiplier(self) -> float:
        """Множитель XP от trust_score (0.8..1.2): точнее оценки — быстрее рост уровня."""
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

    # Для статусных достижений (users/services.py::STATUS_BADGE_TYPES): если показатель
    # упал ниже порога, бейдж остаётся, но помечается неактуальным. Переоценка —
    # revalidate_status_badges_task (раз в месяц).
    is_stale = models.BooleanField(
        _("Утратил актуальность"), default=False,
        help_text=_("Только для статусных достижений — показатель упал ниже порога после получения бейджа."),
    )
    stale_since = models.DateTimeField(_("Утратил актуальность с"), null=True, blank=True)

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

    @property
    def tooltip_text(self) -> str:
        """Текст подсказки для бейджа (с пометкой, если он неактуален)."""
        name = self.get_badge_type_display()
        if self.is_stale:
            return f"{name} — временно неактуально: показатель опустился ниже порога"
        return name


class UserXP(BaseModel):
    """Опыт и уровень пользователя."""

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="xp", verbose_name=_("Пользователь"))
    total_xp = models.IntegerField(_("Всего опыта"), default=0)
    level = models.IntegerField(_("Уровень"), default=1)
    # Дробная часть начислений (0..1) — копится, а не теряется при округлении.
    xp_remainder = models.FloatField(_("Дробный остаток XP"), default=0.0)

    class Meta:
        verbose_name = _("Опыт пользователя")
        verbose_name_plural = _("Опыт пользователей")

    def add_xp(self, amount: float) -> dict:
        """Начисляет XP и пересчитывает уровень. Под select_for_update —
        параллельные начисления не теряются.

        :param amount: XP (обычно уже умножен на xp_multiplier).
        """
        with transaction.atomic():
            locked = UserXP.objects.select_for_update().get(pk=self.pk)

            old_level = locked.level
            old_total_xp = locked.total_xp

            carried = (locked.xp_remainder or 0.0) + amount
            whole = math.floor(carried + 1e-9)
            locked.xp_remainder = max(0.0, carried - whole)
            locked.total_xp = max(0, locked.total_xp + whole)
            new_level = level_for_total_xp(locked.total_xp)

            levels_gained = list(range(old_level + 1, new_level + 1)) if new_level > old_level else []
            locked.level = new_level

            locked.save(update_fields=["level", "total_xp", "xp_remainder", "updated_at"])

        # Синхронизируем инстанс с сохранённым в БД.
        self.total_xp = locked.total_xp
        self.level = locked.level
        self.xp_remainder = locked.xp_remainder

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
        """Прогресс внутри текущего уровня, 0..100."""
        span = self.xp_for_next_level - self.xp_for_current_level
        if span <= 0:
            return 100
        return min(100, max(0, int(((self.total_xp - self.xp_for_current_level) / span) * 100)))

    def __str__(self) -> str:
        return f"{self.user} — Уровень {self.level} ({self.total_xp} XP)"


class SuspiciousActivityFlag(BaseModel):
    """Очередь антифрод-сигналов для модерации.

    Сигналы бывают про пользователя (fast_wizard, ip_cluster, extreme_bias) и
    про сущность — игрока/команду/тренера/судью (vote_spike, *_stats_divergence);
    у сущностных user пустой, а цель — в content_object.
    """

    SOURCE_CHOICES = [
        ("fast_wizard", _("Слишком быстрое заполнение вайзарда оценки")),
        ("ip_cluster", _("Кластер аккаунтов с одного IP")),
        ("extreme_bias", _("Экстремальная историческая предвзятость")),
        ("vote_spike", _("Аномальный всплеск голосования (возможный сговор)")),
        ("stats_divergence", _("Рейтинг команды расходится с объективной статистикой матча")),
        ("player_stats_divergence", _("Рейтинг игрока расходится с объективной статистикой матча")),
        ("coach_stats_divergence", _("Оценки тренера расходятся с игрой его команды")),
        ("manual", _("Отмечено вручную модератором")),
    ]
    STATUS_CHOICES = [
        ("pending", _("Ожидает проверки")),
        ("confirmed", _("Подтверждено — накрутка")),
        ("dismissed", _("Отклонено — ложное срабатывание")),
    ]

    # Пусто у сигналов про сущность.
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="suspicious_activity_flags",
        verbose_name=_("Пользователь"),
    )
    match = models.ForeignKey(
        "matches.Match",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="suspicious_activity_flags",
        verbose_name=_("Матч"),
    )
    # Цель сигнала про сущность (игрок/команда/тренер/судья).
    content_type = models.ForeignKey(
        "contenttypes.ContentType",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        verbose_name=_("Тип сущности"),
    )
    object_id = models.CharField(_("ID сущности"), max_length=64, null=True, blank=True)
    content_object = GenericForeignKey("content_type", "object_id")
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
            models.Index(fields=["content_type", "object_id", "status"]),
        ]
        ordering = ["-score", "-created_at"]

    def __str__(self) -> str:
        subject = self.user or self.content_object or "—"
        return f"{self.get_source_display()} — {subject} ({self.score:.2f})"

    def human_summary(self) -> dict:
        """Объяснение флага для модератора: {explanation, confirm_hint, dismiss_hint}
        (для расхождений со статистикой — ещё what_happened/why/system_action/what_to_do).
        """
        d = self.details or {}

        def pct(x):
            return f"{x:.0%}" if isinstance(x, (int, float)) else "?"

        def num(x, digits=1):
            return round(x, digits) if isinstance(x, (int, float)) else "?"

        subject = str(self.content_object) if self.content_object else (self.user.username if self.user else "—")

        if self.source in ("stats_divergence", "player_stats_divergence", "coach_stats_divergence"):
            return self._divergence_summary(d, subject)

        if self.source == "extreme_bias":
            mean_diff = num(d.get("mean_diff"))
            considered = d.get("considered_matches", "?")
            penalty = d.get("weight_penalty")
            stdev = d.get("diff_stdev")
            explanation = (
                f"За последние {considered} матчей своей команды «{subject}» систематически ставит(ит) "
                f"своим оценки в среднем на {mean_diff} балла выше, чем сопернику."
            )
            if isinstance(stdev, (int, float)) and stdev < 1.0:
                explanation += " Разница почти не меняется от матча к матчу (даже при поражениях своей команды) — не похоже на живую эмоциональную реакцию."
            if isinstance(penalty, (int, float)):
                explanation += f" Вес его/её голоса в общем рейтинге уже автоматически снижен на {penalty:.2f}."
            return {
                "explanation": explanation,
                "confirm_hint": "фиксирует как подтверждённую накрутку/предвзятость — голоса пользователя в этом матче перестают учитываться в рейтинге, решение идёт в калибровку порога",
                "dismiss_hint": "если это обычная искренняя пристрастность фаната (в разумных пределах бывает у всех) — помечает как ложное срабатывание, тоже влияет на будущую калибровку",
            }

        if self.source == "vote_spike" and d.get("compared_matches"):
            # Судья: сравнение с другими матчами лиги.
            explanation = (
                f"Судейство «{subject}» в этом матче оценили {d.get('window_votes', '?')} человек, и "
                f"{pct(d.get('extreme_ratio'))} из них поставили крайние оценки (1-2 или 9-10). Это заметно больше, "
                f"чем обычно бывает у судей в последних {d.get('compared_matches', '?')} матчах лиги. Так выглядит "
                f"массовый призыв «завалить судью» — но и по-настоящему спорное судейство даёт такую же картину."
            )
            return {
                "explanation": explanation,
                "confirm_hint": "если оценки похожи на организованную атаку, а не на реакцию на реальные ошибки — рейтинг напрямую не меняет, помогает точнее настроить детектор",
                "dismiss_hint": "если в матче действительно были спорные решения и реакция болельщиков объяснима",
            }

        if self.source == "vote_spike":
            explanation = (
                f"За последние {d.get('window_hours', '?')} ч у «{subject}» {d.get('window_votes', '?')} "
                f"голосов, из них {pct(d.get('extreme_ratio'))} — крайние оценки (1-2 или 9-10). Заметно "
                f"выделяется на фоне остальных участников этого же матча — похоже на координированный "
                f"призыв проголосовать в соцсетях или чате."
            )
            return {
                "explanation": explanation,
                "confirm_hint": "фиксирует как реальную накрутку — рейтинг напрямую не меняет, но раз в неделю помогает системе точнее подстроить чувствительность этого детектора",
                "dismiss_hint": "если это естественный всплеск эмоций (например, спорное судейское решение) — помечает как ложное срабатывание, тоже влияет на будущую чувствительность детектора",
            }

        if self.source == "ip_cluster":
            explanation = (
                f"{d.get('account_count', '?')} разных аккаунтов завершили оценку одного и того же матча "
                f"с одного IP-адреса за последние {d.get('lookback_hours', '?')} ч — похоже на ферму "
                f"аккаунтов или согласованную группу."
            )
            return {
                "explanation": explanation,
                "confirm_hint": "фиксирует как подтверждённую накрутку — голоса пользователя в этом матче перестают учитываться в рейтинге, решение идёт в калибровку детектора",
                "dismiss_hint": "если это объяснимо (например, семья или общежитие с одним IP) — помечает как ложное срабатывание",
            }

        if self.source == "fast_wizard":
            explanation = (
                f"Визард оценки матча заполнен за {num(d.get('duration_seconds'))} сек — заметно быстрее, "
                f"чем физически успевает обычный человек (минимум — {d.get('threshold_seconds', '?')} сек). "
                f"Похоже на автоматизированное или невнимательное заполнение."
            )
            return {
                "explanation": explanation,
                "confirm_hint": "фиксирует как подтверждённое подозрительное поведение — голоса пользователя в этом матче перестают учитываться в рейтинге",
                "dismiss_hint": "если пользователь объяснил задержку (например, знал матч наизусть) — помечает как ложное срабатывание",
            }

        return {
            "explanation": "Отмечено вручную модератором — подробности в технических деталях ниже.",
            "confirm_hint": "подтверждает сигнал",
            "dismiss_hint": "отклоняет сигнал как ложное срабатывание",
        }

    @property
    def live_correction(self) -> float | None:
        """Текущая авто-поправка сущности (та же, что на её странице), а не снимок из details."""
        if not self.object_id:
            return None
        from aggregates.models import PlayerRatingCorrection, TeamRatingCorrection

        model = {
            "player_stats_divergence": PlayerRatingCorrection,
            "stats_divergence": TeamRatingCorrection,
        }.get(self.source)
        if model is None:
            return None
        fk = "player_id" if model is PlayerRatingCorrection else "team_id"
        return model.objects.filter(**{fk: self.object_id}).values_list("correction", flat=True).first()

    @property
    def score_label(self) -> str:
        """Сила сигнала словами."""
        if self.score >= 0.7:
            return f"сильный сигнал ({self.score:.0%})"
        if self.score >= 0.4:
            return f"средний сигнал ({self.score:.0%})"
        return f"слабый сигнал ({self.score:.0%})"

    def _divergence_summary(self, d: dict, subject: str) -> dict:
        """Объяснение сигнала расхождения со статистикой в 4 блоках:
        что произошло, почему подозрительно, что сделала система, что делать модератору.
        """
        is_player = self.source == "player_stats_divergence"
        pattern = d.get("pattern", "")
        overrated = pattern.startswith("overrated")
        n_raw = d.get("window_matches")
        if isinstance(n_raw, int):
            tail = n_raw % 10
            word = "матч" if tail == 1 and n_raw % 100 != 11 else (
                "матча" if 2 <= tail <= 4 and not 12 <= n_raw % 100 <= 14 else "матчей")
            n = f"{n_raw} {word}"
        else:
            n = "несколько матчей"
        win = d.get("window_avg_rating")
        base = d.get("baseline_avg_rating")
        win_s = f"{win:.1f}" if isinstance(win, (int, float)) else "?"
        base_s = f"{base:.1f}" if isinstance(base, (int, float)) else "?"
        who = f"«{subject}»"

        if is_player:
            facts = (
                "оценка игры по статистике матча" if d.get("objective_source") == "sportmonks_rating"
                else "голы, передачи, отборы, перехваты, единоборства, сейвы, карточки"
            )
        else:
            facts = "удары, удары в створ, опасные атаки, угловые, владение — насколько команда давила на соперника"

        if overrated:
            what_happened = (
                f"Последние {n} болельщики ставили {who} в среднем {win_s} — это выше, чем обычно "
                f"(обычно {base_s}). А по фактам матча ({facts}) {'он' if is_player else 'команда'} в эти же "
                f"матчи сыграл{'' if is_player else 'а'} хуже своего обычного уровня. Оценки пошли вверх, а игра — вниз."
            )
            why = (
                "Так выглядит накрутка «за»: фан-клуб или группа людей массово ставят высокие оценки. "
                "Но бывает и честное объяснение — статистика не всё видит."
            )
        else:
            what_happened = (
                f"Последние {n} болельщики ставили {who} в среднем {win_s} — это ниже, чем обычно "
                f"(обычно {base_s}). А по фактам матча ({facts}) {'он' if is_player else 'команда'} в эти же "
                f"матчи сыграл{'' if is_player else 'а'} лучше своего обычного уровня. Игра пошла вверх, а оценки — вниз."
            )
            why = (
                "Так выглядит накрутка «против»: фанаты соперника или недоброжелатели массово занижают оценки. "
                "Но бывает и честное объяснение — статистика не всё видит."
            )

        live = self.live_correction
        still_active = d.get("pattern_active", True)
        if live is None or abs(live) < 0.01:
            system_action = "Сейчас рейтинг НЕ корректируется: поправка уже затухла до нуля."
        else:
            direction = "повышает" if live > 0 else "понижает"
            system_action = (
                f"Система сама {direction} рейтинг в каждом НОВОМ матче на {abs(live):.2f} балла "
                f"(именно эта цифра видна на странице {'игрока' if is_player else 'команды'}). "
                f"Уже выставленные оценки прошлых матчей не меняются. Поправка небольшая (максимум ±0.4) "
                f"и уменьшается вдвое при каждой ежедневной проверке, если расхождение пропало."
            )
            if (live > 0) == overrated:
                system_action += (
                    " Внимание: направление поправки не совпадает с описанием выше — описание осталось от первого "
                    "сигнала, а картина с тех пор поменялась. При следующей ежедневной проверке описание обновится."
                )
        if not still_active:
            system_action = "Расхождение на последней проверке больше не видно. " + system_action

        if is_player:
            honest_reasons = "травма, игра на непривычной позиции, сильный соперник, или статистика не отражает важный эпизод"
        else:
            honest_reasons = "удаление, травмы ключевых игроков, спорное судейство, игра «от обороны» по плану"
        what_to_do = (
            f"Откройте последние матчи и сверьте с игрой. Если расхождение объяснимо ({honest_reasons}) — "
            f"«Отклонить»: поправка сразу снимется (и с уже посчитанных матчей), и 30 дней система не будет трогать {'этого игрока' if is_player else 'эту команду'}. "
            f"Если согласны, что оценки накручены, — «Подтвердить»: рейтинг это не меняет (поправка уже работает), "
            f"но система учтёт ваше решение и точнее настроит свою чувствительность. Не уверены — можно ничего не делать, "
            f"поправка сама затухнет, если расхождение уйдёт."
        )

        if self.source == "coach_stats_divergence":
            # Тренер: только флаг, без авто-поправки.
            if overrated:
                what_happened = (
                    f"Последние {n} болельщики оценивали тренера {who} в среднем на {win_s} — выше, чем обычно "
                    f"(обычно {base_s}). А его команда в эти матчи объективно уступала соперникам ({facts})."
                )
            else:
                what_happened = (
                    f"Последние {n} болельщики оценивали тренера {who} в среднем на {win_s} — ниже, чем обычно "
                    f"(обычно {base_s}). А его команда в эти матчи объективно превосходила соперников ({facts})."
                )
            system_action = (
                "Оценки тренера автоматически НЕ корректируются — работу тренера по статистике команды можно оценить "
                "только косвенно, поэтому решение оставлено человеку."
            )
            if not still_active:
                system_action = "Расхождение на последней проверке больше не видно. " + system_action
            what_to_do = (
                "Откройте последние матчи команды. Если расхождение объяснимо (замены не сработали, игроки провалили "
                "установку, травмы) — «Отклонить». Если похоже на организованную накрутку — «Подтвердить»: это помогает "
                "системе точнее настраивать чувствительность. Рейтинг тренера ни одна из кнопок не меняет."
            )
            return {
                "explanation": what_happened,
                "what_happened": what_happened,
                "why": why,
                "system_action": system_action,
                "what_to_do": what_to_do,
                "confirm_hint": "рейтинг не меняет, учитывается для настройки чувствительности",
                "dismiss_hint": "помечает как ложное срабатывание",
            }

        return {
            "explanation": what_happened,
            "what_happened": what_happened,
            "why": why,
            "system_action": system_action,
            "what_to_do": what_to_do,
            "confirm_hint": "рейтинг не меняет, только учитывается для настройки чувствительности детектора",
            "dismiss_hint": "сразу снимает авто-поправку (и с прошлых матчей) и выключает проверку на 30 дней",
        }

    # Понятные подписи для ключей details в блоке «Цифры и как считается».
    DETAIL_KEY_LABELS = {
        "pattern": "Что обнаружено",
        "objective_source": "По чему оценивали игру",
        "window_matches": "Сколько последних матчей проверено",
        "window_avg_rating": "Оценка болельщиков в этих матчах",
        "baseline_avg_rating": "Обычная оценка болельщиков (более ранние матчи)",
        "window_avg_objective": "Оценка игры по статистике в этих матчах",
        "baseline_avg_objective": "Обычная оценка игры по статистике",
        "window_avg_dominance_share": "Доля команды в игре (удары, атаки, угловые, владение) в этих матчах",
        "objective_z": "Статистика в сравнении с обычной (0 — как обычно, минус — хуже)",
        "correction_applied": "Поправка на момент проверки",
        "pattern_active": "Расхождение видно на последней проверке",
        "first_detected_at": "Впервые обнаружено",
        "last_checked_at": "Последняя проверка",
        "mean_diff": "Средняя разница оценок (свои − чужие)",
        "diff_stdev": "Разброс разницы от матча к матчу",
        "considered_matches": "Учтено матчей",
        "weight_penalty": "Снижение веса голоса",
        "window_hours": "Окно, часов",
        "window_votes": "Голосов в окне",
        "extreme_ratio": "Доля крайних оценок",
        "account_count": "Аккаунтов",
        "lookback_hours": "Глубина поиска, часов",
        "duration_seconds": "Заполнено за, сек",
        "threshold_seconds": "Порог, сек",
        "session_id": "ID сессии",
        "ip_address": "IP-адрес",
    }

    DETAIL_PATTERN_LABELS = {
        "underrated_despite_dominance": "оценки ниже обычного, хотя команда играла лучше",
        "overrated_despite_poor_play": "оценки выше обычного, хотя команда играла хуже",
        "underrated_despite_stats": "оценки ниже обычного, хотя по статистике играл лучше",
        "overrated_despite_stats": "оценки выше обычного, хотя по статистике играл хуже",
    }

    @property
    def readable_details(self) -> list[tuple[str, str]]:
        """[(подпись, значение)] для details. Неизвестные ключи показываются как есть."""
        result = []
        for key, value in (self.details or {}).items():
            label = self.DETAIL_KEY_LABELS.get(key, key)
            if key == "pattern":
                value = self.DETAIL_PATTERN_LABELS.get(value, value)
            elif key == "objective_source":
                value = {"sportmonks_rating": "оценка по статистике (1–10)", "composite": "наша сумма баллов"}.get(value, value)
            elif key == "pattern_active":
                value = "да" if value else "нет, поправка затухает"
            elif key in ("first_detected_at", "last_checked_at") and isinstance(value, str):
                value = value[:16].replace("T", " ")
            elif isinstance(value, float):
                value = f"{value:+.2f}" if key in ("correction_applied", "mean_diff", "weight_penalty") else round(value, 2)
            result.append((label, value))
        return result


class AntiFraudThreshold(BaseModel):
    """Калибруемые пороги антифрод-детекторов.

    Еженедельная задача recalibrate_antifraud_thresholds двигает порог по доле
    подтверждённых модератором сигналов источника. min_value/max_value —
    жёсткие границы калибровки.
    """

    key = models.CharField(_("Ключ порога"), max_length=64, unique=True)
    value = models.FloatField(_("Текущее действующее значение"))
    default_value = models.FloatField(_("Значение по умолчанию (старт калибровки)"))
    min_value = models.FloatField(_("Нижняя граница калибровки"))
    max_value = models.FloatField(_("Верхняя граница калибровки"))
    last_note = models.CharField(
        _("Причина последнего изменения"), max_length=255, blank=True,
        help_text=_("Заполняется автоматически задачей пересчёта — для прозрачности в admin."),
    )

    class Meta:
        verbose_name = _("Порог антифрода")
        verbose_name_plural = _("Пороги антифрода (самокалибровка)")
        ordering = ["key"]

    def __str__(self) -> str:
        return f"{self.key} = {self.value}"


class Follow(BaseModel):
    """Подписка пользователя на игрока или команду. Заполнено ровно одно поле —
    гарантирует CheckConstraint.
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
                # condition=, а не check= (Django 6).
                condition=(
                    models.Q(player__isnull=False, team__isnull=True)
                    | models.Q(player__isnull=True, team__isnull=False)
                ),
                name='follow_exactly_one_target',
            ),
        ]
        indexes = [
            # Явное имя индекса — миграции пишутся вручную.
            models.Index(fields=['user'], name='follow_user_idx'),
        ]

    def __str__(self) -> str:
        target = self.player or self.team
        return f"{self.user.username} → {target}"


class PushSubscription(BaseModel):
    """Push-подписка браузера. У пользователя может быть несколько (разные устройства),
    уникален endpoint.
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

    @property
    def friendly_label(self) -> str:
        """«Chrome · macOS» из user_agent — для списка устройств в настройках уведомлений."""
        if not self.user_agent:
            return 'Неизвестное устройство'
        try:
            from user_agents import parse as parse_ua
            ua = parse_ua(self.user_agent)
        except Exception:
            return 'Неизвестное устройство'

        browser = ua.browser.family or 'Браузер'
        os_name = ua.os.family or ''
        if ua.is_bot:
            return 'Бот/скрипт'
        label = f"{browser} · {os_name}" if os_name else browser
        if ua.is_mobile:
            label += ' (телефон)'
        elif ua.is_tablet:
            label += ' (планшет)'
        return label