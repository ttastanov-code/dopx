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


class ExpertTake(BaseModel):
    """Мнение экспертов DOPX о матче — показывается, пока голосов болельщиков мало."""

    match = models.ForeignKey("matches.Match", on_delete=models.CASCADE, related_name="expert_takes")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    author_title = models.CharField(_("Подпись автора"), max_length=80, default="Редакция DOPX")
    text = models.TextField(_("Мнение"), max_length=600)
    key_player = models.ForeignKey(
        "players.Player", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
        verbose_name=_("Ключевой игрок"),
    )
    is_published = models.BooleanField(_("Опубликовано"), default=True)

    class Meta:
        verbose_name = _("Мнение эксперта")
        verbose_name_plural = _("Мнения экспертов")
        ordering = ["-created_at"]


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
