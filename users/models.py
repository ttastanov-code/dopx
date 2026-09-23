# users/models.py
"""
User.registration_ip/registration_user_agent — для сопоставления кластеров
аккаунтов с одного IP (см. users/views.py::RegisterView). UserBadge.BADGE_TYPES
строится из единого каталога users/badges.py; rarity/is_secret/description —
properties поверх каталога, не колонки БД. UserXP — возрастающая (треугольная)
кривая уровней, progress_percent считается от реальных границ текущего и
следующего уровня, а не total_xp % 100. SuspiciousActivityFlag — очередь
ручной модерации антифрод-сигналов поверх trust_score (см. users/admin.py).

Требует `python manage.py makemigrations users` после применения.
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
    # ИСПРАВЛЕНО (2026-09-23, продуктовый запрос после жалобы на
    # leaderboard — "город у нас необязательное текстовое поле, можно
    # убрать, либо доработать чтобы были реальные города всех
    # пользователей"): было свободным текстом без вариантов — опечатки и
    # разное написание одного города ("Алматы"/"алматы"/"Алма-Ата")
    # засоряли выпадающий фильтр городов на leaderboard (users/views.py::
    # UserLeaderboardView.available_cities). choices — см. users/
    # kz_cities.py за полным списком и объяснением, почему "авто-
    # актуализация" здесь означает ручное обновление одного файла-
    # справочника, а не живую синхронизацию с госреестром (такого API не
    # существует). blank=True на уровне поля ОСТАЁТСЯ (не переносим сюда
    # required — это фильтруется формой регистрации, см.
    # users/forms.py::RegisterForm, чтобы у уже существующих
    # пользователей со старым свободным текстом в city ничего не
    # сломалось на save()).
    city = models.CharField(_("Город"), max_length=120, blank=True, choices=KZ_CITY_CHOICES)
    rating_power = models.FloatField(_("Сила рейтинга"), default=1.0)
    trust_score = models.FloatField(_("Оценка доверия"), default=1.0)
    is_verified = models.BooleanField(_("Верифицирован"), default=False)
    # default=True — не ломает уже расшаренные ссылки на существующие профили.
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

    # Серия оценок считается по турам чемпионата, не по календарным дням.
    # См. docs/adr/0022-streak-semantics-redesign.md.
    evaluation_streak = models.IntegerField(_("Серия оценок"), default=0)
    # UUIDField, не IntegerField — Season.id UUID, не autoincrement integer.
    # См. docs/adr/0022-streak-semantics-redesign.md, дополнение.
    last_evaluation_season_id = models.UUIDField(
        _("Сезон последней оценки"), null=True, blank=True
    )
    last_evaluation_tour = models.PositiveSmallIntegerField(
        _("Тур последней оценки"), null=True, blank=True
    )

    # Серия угаданных исходов подряд, не дней активности — считается при
    # завершении матча (final_result), не в момент ставки. См.
    # docs/adr/0022-streak-semantics-redesign.md.
    prediction_streak = models.IntegerField(_("Серия угаданных прогнозов"), default=0)

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
        # НОВОЕ (2026-08-21) — 4 петли удержания, см.
        # docs/BACKLOG.md и notifications/tasks.py:
        "email_prediction_closing": True,  # loop 1: скоро закроется приём прогнозов на матч
        "email_weekly_summary": True,       # loop 2: персональная сводка недели
        "email_prediction_result": True,    # loop 3: ваш прогноз vs исход/сообщество
        # НОВОЕ (2026-08-22): итоги «DOPX Лучшие тура» — письмо при
        # автоматической финализации тура (round_squad/services.py).
        "email_round_results": True,
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

    def update_evaluation_stats(self, match) -> None:
        """
        Обновляет статистику оценок. Достижения проверяются отдельно.

        `match` — матч, который только что оценили (нужны его `season_id`/
        `tour` для серии по турам, см. докстринг `evaluation_streak` выше).
        Если у матча не проставлен `tour` (бывает у кубковых/переносимых
        матчей, ещё не досинхронизированных с KFF) — серию НЕ трогаем
        вообще (ни +1, ни сброс): нет надёжной единицы сравнения, а молча
        обнулять серию из-за дыры в данных парсера было бы несправедливо
        по отношению к пользователю.
        """
        self.total_evaluations += 1
        tour = match.tour
        if tour is not None:
            same_season = self.last_evaluation_season_id == match.season_id
            if same_season and self.last_evaluation_tour == tour:
                pass  # тот же тур — уже засчитан, серию не трогаем
            elif (
                same_season
                and self.last_evaluation_tour is not None
                and tour == self.last_evaluation_tour + 1
            ):
                self.evaluation_streak += 1  # следующий тур подряд в том же сезоне
                self.last_evaluation_tour = tour
            elif (
                same_season
                and self.last_evaluation_tour is not None
                and tour < self.last_evaluation_tour
            ):
                # 2026-09-23, фикс аудита: пользователь оценил тур МЕНЬШЕ
                # уже засчитанного максимума — например, наверстал
                # пропущенный или перенесённый матч из более раннего тура
                # уже ПОСЛЕ того, как оценил более поздний. Раньше
                # `last_evaluation_tour` перезаписывался этим меньшим
                # значением всегда, из-за чего следующая оценка реально
                # следующего по порядку тура (например, снова тур+1 от
                # текущего максимума) считалась "разрывом" и сбрасывала
                # серию — хотя последовательность туров у пользователя была
                # почти непрерывной. `last_evaluation_tour` — это МАКСИМУМ
                # оценённого тура, а не "последний по времени клика", поэтому
                # ни серию, ни максимум тут трогать не нужно: тур уже входит
                # в диапазон, +1 к серии он не даёт (не продолжение), но и
                # не разрывает её.
                pass
            else:
                self.evaluation_streak = 1  # разрыв, смена сезона или первая оценка
                self.last_evaluation_tour = tour
            self.last_evaluation_season_id = match.season_id
        self.save(update_fields=[
            "total_evaluations", "evaluation_streak",
            "last_evaluation_season_id", "last_evaluation_tour", "updated_at",
        ])

    def update_prediction_stats(self, is_correct: bool) -> None:
        """
        Обновляет `prediction_streak` — ТЕКУЩУЮ беспрерывную серию УГАДАННЫХ
        прогнозов 1X2 подряд (см. докстринг `prediction_streak` выше).

        Вызывается ТОЛЬКО из `notifications/tasks.py::notify_prediction_results`,
        когда у матча уже точно известен исход (`status='finished'`) — НЕ из
        `predictions/views.py` в момент самой ставки, там `is_correct` ещё
        не может быть определён. Матчи в этой задаче обрабатываются в
        порядке `end_time` — важно для правильного порядка +1/сброса, если
        у пользователя в одном прогоне сразу несколько свежезавершённых
        матчей.
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

    # 2026-09-23, фикс аудита: часть достижений (STATUS_BADGE_TYPES в
    # users/services.py — foresight/max_trust/stable_hand/accurate_analyst/
    # bias_free) — это не разовая веха ("оценил 10 матчей", навсегда), а
    # утверждение о ТЕКУЩЕМ качестве пользователя (высокий устойчивый
    # trust_score, высокая точность прогнозов и т.п.). Раньше такие бейджи
    # выдавались один раз и оставались в профиле навсегда, даже если
    # показатель давно упал ниже порога — periodic-задача
    # revalidate_status_badges_task (users/tasks.py) периодически
    # перепроверяет условие ТОЛЬКО для этих 5 типов и помечает бейдж
    # is_stale=True, если условие больше не выполняется (сам объект НЕ
    # удаляется — это по-прежнему реальное историческое достижение,
    # которое пользователь заслужил, просто больше не отражает текущее
    # состояние). Если показатель восстановится — is_stale снова снимается
    # автоматически, повторно бейдж не выдаётся (get_or_create и так не
    # создал бы дубликат). Для остальных типов бейджей (разовые вехи) это
    # поле всегда False и никогда не проверяется.
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
        """
        2026-09-23, фикс аудита: текст для {% tooltip_wrap %} в профиле —
        вынесен сюда, а не собран прямо в шаблоне через {% if %}, потому
        что tooltip_wrap — блочный тег (`parser.parse(("endtooltip_wrap",))`,
        core/templatetags/tooltip_tags.py), который жадно поглощает все
        токены до своего endtooltip_wrap; условная развилка МЕЖДУ двумя
        разными открывающими {% tooltip_wrap %} внутри {% if %}/{% else %}
        ломает парсинг шаблона (второй tooltip_wrap оказывается "внутри"
        nodelist первого). Один тег — один вычисленный текст.
        """
        name = self.get_badge_type_display()
        if self.is_stale:
            return f"{name} — временно неактуально: показатель опустился ниже порога"
        return name


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

        БАГ, КОТОРЫЙ ТУТ БЫЛ: read-modify-write без блокировки — `self.total_xp`
        читался из уже полученного (возможно устаревшего) инстанса и сразу
        сохранялся обратно. При двух параллельных начислениях одному и тому же
        пользователю (например, два шага вайзарда, обработанных гонкой
        воркеров) оба процесса стартовали с одинакового total_xp и оба
        сохраняли свой результат — выигрывал тот, кто сохранил последним, а
        второе начисление молча терялось. Теперь фактическое
        чтение+изменение+запись всегда идёт под `select_for_update()` внутри
        `transaction.atomic()` по актуальной строке из БД, а не по тому, что
        было в `self` на момент вызова.
        """
        with transaction.atomic():
            locked = UserXP.objects.select_for_update().get(pk=self.pk)

            old_level = locked.level
            old_total_xp = locked.total_xp

            locked.total_xp = max(0, locked.total_xp + int(round(amount)))
            new_level = level_for_total_xp(locked.total_xp)

            levels_gained = list(range(old_level + 1, new_level + 1)) if new_level > old_level else []
            locked.level = new_level

            locked.save(update_fields=["level", "total_xp", "updated_at"])

        # Синхронизируем текущий (возможно, устаревший) инстанс с тем, что
        # реально сохранено в БД — вызывающий код читает self.total_xp/self.level
        # сразу после add_xp() без явного refresh_from_db() (см. users/tests.py).
        self.total_xp = locked.total_xp
        self.level = locked.level

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
        Прогресс внутри текущего уровня, 0..100 — от реальных границ
        текущего/следующего уровня, не total_xp % 100 (это верно только
        для фиксированного шага, а кривая уровней — возрастающая).
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
    По конкретному источнику (скорость заполнения вайзарда, IP-кластер и
    т.д.) — непрерывный score и статус ручного разбора.

    2026-08-23, anti-brigading: добавлен источник "vote_spike" (см.
    aggregates/tasks.py::detect_vote_velocity_anomalies_task) — сигнал не
    про КОНКРЕТНОГО пользователя, а про АНОМАЛИЮ У СУЩНОСТИ (игрок/
    команда/тренер получили статистически выбивающийся всплеск
    экстремальных оценок в коротком окне — сигнатура координированного
    призыва в соцсетях/телеграм-чате, а не бот-фермы или предвзятого
    индивида). Поэтому:
    - `user` стал nullable — entity-level флаги не привязаны к одному
      пользователю (наоборот, к МНОЖЕСТВУ голосовавших).
    - Добавлен generic FK (content_type/object_id/content_object) на
      сущность — Player/Team/Coach (используется content_type-агностично,
      как в round_squad.RoundCandidate, тот же паттерн в проекте).

    2026-08-23, источник "stats_divergence" (aggregates/tasks.py::
    detect_rating_stats_divergence_task): единственный источник этого
    флага, который сравнивает рейтинг сообщества не с самим собой
    (остальными голосами/историей пользователя), а с ОБЪЕКТИВНЫМИ фактами
    матча (matches.models.MatchTeamStatistics) — не зависит от голосов
    DOPX вообще, поэтому его нельзя обмануть, просто договорившись ставить
    "умеренные" оценки (см. VOTE_SPIKE/extreme_bias/градуированный штраф
    веса в aggregates/services.py — все они так или иначе смотрят на сами
    голоса). Как и vote_spike — сущность (Team), не пользователь.
    2026-09-08: источник данных статистики сменился с KFF на Sportmonks
    (docs/sportmonks-migration-plan.md) — MatchTeamStatistics осталась той
    же моделью, менять тут нечего, кроме формулировки в SOURCE_CHOICES
    ниже (была явно "...от KFF", больше не соответствует действительности).

    2026-09-08, источник "player_stats_divergence" (aggregates/tasks.py::
    detect_player_rating_stats_divergence_task) — тот же принцип, что у
    "stats_divergence", но на уровне ИГРОКА и с другим объективным
    сигналом (MatchPlayerStatistics + события матча вместо "доли
    доминирования" — см. докстринг aggregates.models.PlayerRatingCorrection
    про то, почему у игрока нет прямого аналога team dominance share).
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

    # null=True — см. докстринг класса: entity-level флаги (vote_spike) не
    # привязаны к одному пользователю.
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
    # Generic FK на сущность (Player/Team/Coach) — только для entity-level
    # сигналов (vote_spike). Оба поля null=True/blank=True — user-level
    # сигналы (fast_wizard/ip_cluster/extreme_bias) их не используют.
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
        """
        Человеко-читаемое объяснение флага — раньше дашборд просто дампил
        сырой `details` dict построчно (`pattern: underrated_despite_
        dominance`, `window_matches: 8`...). Живой фидбэк пользователя
        2026-08-24: "если посадить человека со стороны и сказать модерируй,
        он ничего не поймёт". Возвращает {"explanation", "confirm_hint",
        "dismiss_hint"} — специфично для source, потому что смысл и РЕАЛЬНЫЕ
        последствия кнопок разные у разных сигналов: например, у
        stats_divergence "Отклонить" в самом деле снимает автопоправку
        рейтинга (users/admin.py::mark_dismissed), а у остальных источников
        решение рейтинг напрямую не трогает — только копится в статистике
        для еженедельной самокалибровки порогов (recalibrate_antifraud_
        thresholds). Явно говорим об этом в hint'ах, а не оставляем
        человека додумывать.
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
            # 2026-09-23, честный аудит формул рейтингов — раньше этот
            # источник был объявлен в SOURCE_CHOICES, но нигде в коде не
            # создавался (мёртвый выбор). Теперь aggregates/services.py::
            # _maybe_flag_extreme_bias заводит флаг при заметном
            # градуированном штрафе веса голоса за систематическую
            # пристрастность — см. её докстринг и _graduated_bias_penalty.
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
                "confirm_hint": "фиксирует как подтверждённую накрутку/предвзятость — помогает системе точнее калибровать порог видимости этого сигнала на будущее",
                "dismiss_hint": "если это обычная искренняя пристрастность фаната (в разумных пределах бывает у всех) — помечает как ложное срабатывание, тоже влияет на будущую калибровку",
            }

        if self.source == "vote_spike" and d.get("compared_matches"):
            # 2026-09-24: судья — сравнение не с соседями по матчу, а с
            # другими недавними матчами лиги (aggregates/tasks.py::detect_referee_vote_spikes_task).
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
                "confirm_hint": "фиксирует как подтверждённую накрутку — помогает системе точнее калибровать чувствительность детектора на будущее",
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
                "confirm_hint": "фиксирует как подтверждённое подозрительное поведение этого пользователя",
                "dismiss_hint": "если пользователь объяснил задержку (например, знал матч наизусть) — помечает как ложное срабатывание",
            }

        return {
            "explanation": "Отмечено вручную модератором — подробности в технических деталях ниже.",
            "confirm_hint": "подтверждает сигнал",
            "dismiss_hint": "отклоняет сигнал как ложное срабатывание",
        }

    @property
    def live_correction(self) -> float | None:
        """ТЕКУЩАЯ авто-поправка сущности (из PlayerRatingCorrection/
        TeamRatingCorrection), а не снимок из details на момент создания
        флага. Именно её видит пользователь на странице игрока/команды —
        раньше модератор видел −0.23 из старого снимка, а на профиле
        висело +0.25 (жалоба 2026-09-23)."""
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
        """Вместо непонятного "score 0,57" — словами."""
        if self.score >= 0.7:
            return f"сильный сигнал ({self.score:.0%})"
        if self.score >= 0.4:
            return f"средний сигнал ({self.score:.0%})"
        return f"слабый сигнал ({self.score:.0%})"

    def _divergence_summary(self, d: dict, subject: str) -> dict:
        """
        2026-09-23, жалоба пользователя: "все эти тексты нихуя непонятны —
        что произошло, почему антифрод сработал, что делать дальше". Текст
        разбит на 4 блока простым языком: что произошло / почему это
        подозрительно / что система уже сделала (по ТЕКУЩЕЙ поправке, а не
        по устаревшему снимку) / что делать модератору.
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
            f"«Отклонить»: поправка сразу снимется, и 30 дней система не будет трогать {'этого игрока' if is_player else 'эту команду'}. "
            f"Если согласны, что оценки накручены, — «Подтвердить»: рейтинг это не меняет (поправка уже работает), "
            f"но система учтёт ваше решение и точнее настроит свою чувствительность. Не уверены — можно ничего не делать, "
            f"поправка сама затухнет, если расхождение уйдёт."
        )

        if self.source == "coach_stats_divergence":
            # 2026-09-24: тренер — оценивается против игры ЕГО команды, авто-поправки нет.
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
            "dismiss_hint": "сразу снимает авто-поправку и выключает проверку на 30 дней",
        }

    # 2026-09-23, честный аудит формул рейтингов (прямая жалоба пользователя
    # на сырой дамп деталей вида "pattern: underrated_despite_stats
    # objective_z: 0,64 window_matches: 3..." под сворачивающимся блоком
    # "Технические детали" в dashboard/antifraud.html): human_summary выше
    # уже даёт связное объяснение на естественном языке, но само окно с
    # ключами details ПОД ним всё равно дампилось как есть — понятно только
    # тому, кто читал этот же код. Единый словарь меток по ВСЕМ ключам,
    # которые когда-либо пишет любой источник (aggregates/tasks.py,
    # users/tasks.py), чтобы технический блок был вспомогательным
    # уточнением для модератора-эксперта, а не единственным источником
    # смысла для рядового модератора.
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
        """[(человеко-читаемая метка, отформатированное значение), ...] —
        см. комментарий у DETAIL_KEY_LABELS выше. Неизвестный ключ (на
        случай будущего детектора, для которого метку забыли завести)
        показывается как есть, а не прячется — лучше некрасиво, чем молча
        потерять информацию."""
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
    """
    Самокалибрующиеся пороги детекторов накрутки — 2026-08-23, продуктовый
    запрос "пороги не должны быть высечены в коде навечно — тот, кто
    прочитает исходники, получает готовую инструкцию, как оставаться
    чуть ниже границы обнаружения". Хранит ТЕКУЩЕЕ действующее значение
    порога; еженедельная задача `users.tasks.recalibrate_antifraud_thresholds`
    подстраивает его на основе РЕАЛЬНЫХ решений модератора (доля
    подтверждённых накруток против отклонённых как ложная тревога у
    сигналов этого источника) — не наугад и не "просто чтобы двигалось",
    а по фактической точности сигнала.

    Калибруется НЕ каждый порог в проекте: адаптация имеет смысл только
    там, где есть земля под ногами — разобранные модератором флаги с
    вердиктом confirmed/dismissed (`SuspiciousActivityFlag.status`). У
    `vote_spike` и `ip_cluster` такая обратная связь есть.

    ИСПРАВЛЕНО (2026-09-23, честный аудит формул рейтингов): до этой даты
    у градуированного штрафа за предвзятость (`aggregates/services.py::
    _graduated_bias_penalty`) обратной связи не было вообще — штраф
    применялся молча, без единого следа в очереди модерации, значит и
    калибровать было нечего. Теперь заметный штраф (см.
    `EXTREME_BIAS_FLAG_THRESHOLD`) создаёт `SuspiciousActivityFlag(
    source="extreme_bias")` — появилась земля под ногами, и ПОРОГ
    ВИДИМОСТИ (с какого штрафа заводить флаг, не сами константы формулы
    штрафа BIAS_FREE_DIFF/BIAS_MAX_DIFF) калибруется точно так же, как
    vote_spike/ip_cluster (ключ `extreme_bias_flag_threshold`).

    `min_value`/`max_value` — жёсткие границы, за которые калибровка не
    может выйти, даже если решения модератора массово смещены (случайно
    или намеренно): без них порог можно было бы медленно сдвинуть,
    систематически подсовывая модератору пограничные случаи.
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

    @property
    def friendly_label(self) -> str:
        """
        Человекочитаемое "Chrome · macOS" вместо сырого user_agent — для
        страницы настроек уведомлений (2026-08-31, по запросу пользователя
        после реального инцидента: подписка с Chrome на Mac молча висела в
        БД, пользователь зашёл через Safari на том же Mac и не мог понять,
        откуда взялось "устройство подключено" на странице настроек — это
        было не багом статуса текущего браузера (тот и так проверяется
        честно через PushManager.getSubscription(), см. докстринг в
        notification_settings.html), а отсутствием видимости СПИСКА чужих
        подписок: пользователь не мог посмотреть, что именно подписано, и
        отключить конкретное устройство, не трогая своё текущее.

        Ленивый импорт `user_agents` — та же тяжёлая либа с regex-базой
        ua-parser, что уже используется в analytics/selectors.py::
        _device_breakdown для трафика, тот же паттерн (не грузим на каждый
        импорт users/models.py, если админка/дашборд её не смотрит).
        """
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