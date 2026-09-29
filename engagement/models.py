# engagement/models.py
"""Механики удержания и роста: серия дней, ежедневные задания, сезонный пропуск,
приглашения, лиги прогнозистов с друзьями, мнения экспертов DOPX."""
from __future__ import annotations

import secrets

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from core.models import BaseModel


def _invite_code() -> str:
    return secrets.token_urlsafe(6).replace("-", "").replace("_", "")[:8].upper()


class DailyStreak(BaseModel):
    """Серия дней с активностью на сайте; заморозка закрывает пропущенный день (как в Duolingo)."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="daily_streak")
    current = models.PositiveIntegerField(_("Текущая серия"), default=0)
    best = models.PositiveIntegerField(_("Лучшая серия"), default=0)
    last_active_date = models.DateField(_("Последний активный день"), null=True, blank=True)
    freezes = models.PositiveSmallIntegerField(_("Заморозки"), default=0)
    freezes_used = models.PositiveIntegerField(_("Использовано заморозок"), default=0)

    class Meta:
        verbose_name = _("Серия дней")
        verbose_name_plural = _("Серии дней")


class DailyQuest(BaseModel):
    """Задание дня пользователя; ключи и награды — engagement/quests.py."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="daily_quests")
    date = models.DateField(_("День"))
    key = models.CharField(_("Задание"), max_length=40)
    target = models.PositiveSmallIntegerField(_("Цель"), default=1)
    progress = models.PositiveSmallIntegerField(_("Прогресс"), default=0)
    xp_reward = models.PositiveSmallIntegerField(_("Награда XP"), default=10)
    completed_at = models.DateTimeField(_("Выполнено"), null=True, blank=True)

    class Meta:
        verbose_name = _("Задание дня")
        verbose_name_plural = _("Задания дня")
        constraints = [models.UniqueConstraint(fields=["user", "date", "key"], name="unique_daily_quest")]
        indexes = [models.Index(fields=["user", "date"])]

    @property
    def is_done(self) -> bool:
        return self.completed_at is not None


class SeasonPass(BaseModel):
    """Сезонный прогресс: XP за сезон, 30 уровней с наградами; каждый сезон — с нуля."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="season_passes")
    season = models.ForeignKey("seasons.Season", on_delete=models.CASCADE, related_name="passes")
    xp = models.PositiveIntegerField(_("XP сезона"), default=0)
    claimed_levels = models.JSONField(_("Выданные награды (уровни)"), default=list, blank=True)

    class Meta:
        verbose_name = _("Сезонный пропуск")
        verbose_name_plural = _("Сезонные пропуски")
        constraints = [models.UniqueConstraint(fields=["user", "season"], name="unique_season_pass")]
        indexes = [models.Index(fields=["season", "-xp"])]


class ReferralCode(BaseModel):
    """Личная ссылка-приглашение пользователя: /r/<code>/."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="referral_code")
    code = models.CharField(_("Код"), max_length=12, unique=True, default=_invite_code)

    class Meta:
        verbose_name = _("Код приглашения")
        verbose_name_plural = _("Коды приглашений")


class Referral(BaseModel):
    """Кто кого пригласил; награда — когда приглашённый завершил первую оценку матча."""

    SOURCE_LINK = "link"
    SOURCE_CHALLENGE = "challenge"
    SOURCE_LEAGUE = "league"
    SOURCE_CHOICES = [
        (SOURCE_LINK, _("Ссылка-приглашение")),
        (SOURCE_CHALLENGE, _("Вызов на прогноз")),
        (SOURCE_LEAGUE, _("Лига прогнозистов")),
    ]

    inviter = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="referrals_sent")
    invited = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="referred_by")
    source = models.CharField(_("Источник"), max_length=20, choices=SOURCE_CHOICES, default=SOURCE_LINK)
    rewarded_at = models.DateTimeField(_("Награда выдана"), null=True, blank=True)

    class Meta:
        verbose_name = _("Приглашение")
        verbose_name_plural = _("Приглашения")


class FriendLeague(BaseModel):
    """Лига прогнозистов с друзьями: очки за угаданные исходы матчей сезона после создания лиги."""

    name = models.CharField(_("Название"), max_length=60)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="owned_friend_leagues")
    season = models.ForeignKey("seasons.Season", on_delete=models.CASCADE, related_name="friend_leagues")
    invite_code = models.CharField(_("Код приглашения"), max_length=12, unique=True, default=_invite_code)
    members = models.ManyToManyField(settings.AUTH_USER_MODEL, through="FriendLeagueMember", related_name="friend_leagues")

    class Meta:
        verbose_name = _("Лига прогнозистов")
        verbose_name_plural = _("Лиги прогнозистов")

    def __str__(self):
        return self.name


class FriendLeagueMember(BaseModel):
    league = models.ForeignKey(FriendLeague, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="friend_league_memberships")

    class Meta:
        verbose_name = _("Участник лиги")
        verbose_name_plural = _("Участники лиг")
        constraints = [models.UniqueConstraint(fields=["league", "user"], name="unique_friend_league_member")]


class Expert(BaseModel):
    """Эксперт DOPX: имя, роль и фото вводятся один раз и подставляются в мнения."""

    name = models.CharField(_("Имя"), max_length=80)
    title = models.CharField(_("Кто это"), max_length=120, blank=True,
                             help_text=_("Коротко: «экс-игрок сборной Казахстана», «тренер UEFA A»"))
    photo = models.ImageField(_("Фото"), upload_to="experts/", blank=True)
    is_active = models.BooleanField(_("Активен"), default=True)

    class Meta:
        verbose_name = _("Эксперт")
        verbose_name_plural = _("Эксперты")
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def initials(self) -> str:
        return "".join(part[0] for part in self.name.split()[:2]).upper()


def _invite_token() -> str:
    import secrets

    return secrets.token_urlsafe(24)


class ExpertInvite(BaseModel):
    """Ссылка для эксперта: пишет мнение без регистрации, пока ссылка жива."""

    token = models.CharField(max_length=64, unique=True, default=_invite_token, editable=False)
    # Пусто — эксперт представится сам, при первом мнении создастся карточка.
    expert = models.ForeignKey(Expert, on_delete=models.SET_NULL, null=True, blank=True, related_name="invites",
                               verbose_name=_("Эксперт"))
    # Пусто — эксперт сам выберет недавний матч.
    match = models.ForeignKey("matches.Match", on_delete=models.CASCADE, null=True, blank=True,
                              related_name="expert_invites", verbose_name=_("Матч"))
    expires_at = models.DateTimeField(_("Действует до"))
    max_takes = models.PositiveSmallIntegerField(_("Сколько мнений можно написать"), default=1)
    auto_publish = models.BooleanField(_("Публиковать без проверки"), default=False)
    note = models.CharField(_("Заметка для себя"), max_length=120, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    revoked_at = models.DateTimeField(null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = _("Ссылка для эксперта")
        verbose_name_plural = _("Ссылки для экспертов")
        ordering = ["-created_at"]

    def __str__(self):
        return self.expert.name if self.expert else (self.note or "Ссылка для эксперта")

    def takes_used(self) -> int:
        return self.takes.count()

    def status(self) -> str:
        """active / expired / revoked / used."""
        from django.utils import timezone

        if self.revoked_at:
            return "revoked"
        if self.expires_at <= timezone.now():
            return "expired"
        if self.takes_used() >= self.max_takes:
            return "used"
        return "active"


class ExpertTake(BaseModel):
    """Мнение эксперта о матче. Пока идёт голосование — только тем, кто уже оценил матч."""

    match = models.ForeignKey("matches.Match", on_delete=models.CASCADE, related_name="expert_takes")
    expert = models.ForeignKey(Expert, on_delete=models.SET_NULL, null=True, blank=True, related_name="takes",
                               verbose_name=_("Эксперт"))
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    # Подпись, если эксперт не выбран.
    author_title = models.CharField(_("Подпись автора"), max_length=80, default="Редакция DOPX")
    headline = models.CharField(_("Главная мысль"), max_length=140, blank=True,
                                help_text=_("Одна фраза крупно над текстом"))
    text = models.TextField(_("Мнение"), max_length=3000)
    key_player = models.ForeignKey(
        "players.Player", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
        verbose_name=_("Ключевой игрок"),
    )
    is_published = models.BooleanField(_("Опубликовано"), default=True)
    invite = models.ForeignKey(ExpertInvite, on_delete=models.SET_NULL, null=True, blank=True, related_name="takes",
                               verbose_name=_("Прислано по ссылке"))

    class Meta:
        verbose_name = _("Мнение эксперта")
        verbose_name_plural = _("Мнения экспертов")
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.display_name}: {self.match}"

    @property
    def display_name(self) -> str:
        return self.expert.name if self.expert else self.author_title

    @property
    def display_title(self) -> str:
        return self.expert.title if self.expert else ""

    @property
    def initials(self) -> str:
        return self.expert.initials if self.expert else "D"

    @property
    def is_preview(self) -> bool:
        """Написано до начала матча."""
        return bool(self.created_at) and self.created_at < self.match.start_time

    @property
    def is_long(self) -> bool:
        return len(self.text) > LONG_TAKE_CHARS


# Длиннее — текст свёрнут, «Читать полностью».
LONG_TAKE_CHARS = 420


class DailyPoll(BaseModel):
    """Опрос недели: «Спорный момент» (эпизод прошлого тура) или «Дуэль» двух игроков тура."""

    KIND_EPISODE = "episode"
    KIND_DUEL = "duel"
    KIND_CHOICES = [(KIND_EPISODE, _("Спорный момент")), (KIND_DUEL, _("Дуэль тура"))]

    kind = models.CharField(_("Тип"), max_length=10, choices=KIND_CHOICES)
    match = models.ForeignKey("matches.Match", on_delete=models.CASCADE, related_name="daily_polls")
    event = models.ForeignKey("events.MatchEvent", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    player_a = models.ForeignKey("players.Player", on_delete=models.CASCADE, null=True, blank=True, related_name="+")
    player_b = models.ForeignKey("players.Player", on_delete=models.CASCADE, null=True, blank=True, related_name="+")
    question = models.CharField(_("Вопрос"), max_length=160)
    context = models.CharField(_("Контекст"), max_length=255, blank=True)
    option_a = models.CharField(_("Вариант A"), max_length=80)
    option_b = models.CharField(_("Вариант B"), max_length=80)
    # Подписи под вариантами дуэли: клуб, оценка, голы.
    detail_a = models.CharField(max_length=120, blank=True)
    detail_b = models.CharField(max_length=120, blank=True)
    score = models.FloatField(_("Индекс спорности"), default=0.0)
    closes_at = models.DateTimeField(_("Закрывается"))

    class Meta:
        verbose_name = _("Опрос недели")
        verbose_name_plural = _("Опросы недели")
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_kind_display()}: {self.question}"


class DailyPollVote(BaseModel):
    CHOICES = [("a", "A"), ("b", "B")]

    poll = models.ForeignKey(DailyPoll, on_delete=models.CASCADE, related_name="votes")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="poll_votes")
    choice = models.CharField(max_length=1, choices=CHOICES)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["poll", "user"], name="unique_poll_vote")]
