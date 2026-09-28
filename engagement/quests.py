# engagement/quests.py
"""Задания дня: три из пула, только те, что реально можно выполнить сегодня.
Прогресс — engagement/signals.py (прогнозы, оценки, реакции) и track() во вьюхах (визиты)."""
from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import timedelta
from typing import Callable

from django.db import IntegrityError, transaction
from django.utils import timezone

QUESTS_PER_DAY = 4
ALL_DONE_KEY = "all_done"
ALL_DONE_XP = 30


@dataclass(frozen=True)
class QuestDef:
    key: str
    title: str
    icon: str
    xp: int
    target: Callable  # user -> int (0 — недоступно сегодня)
    url_name: str = ""


def _votable_count(user) -> int:
    from core.personal import votable_matches_for

    return votable_matches_for(user).count()


def _predictable_count(user) -> int:
    from matches.models import Match
    from predictions.models import MatchPrediction

    now = timezone.now()
    return min(2, Match.objects.filter(
        status="scheduled", start_time__gt=now,
        start_time__lte=now + timedelta(days=Match.PREDICTION_WINDOW_DAYS),
    ).exclude(id__in=MatchPrediction.objects.filter(user=user).values("match_id")).count())


def _react_count(user) -> int:
    from matches.models import Match, MatchReaction

    recent = Match.objects.filter(status="finished", end_time__gte=timezone.now() - timedelta(days=10))
    return min(2, recent.exclude(id__in=MatchReaction.objects.filter(user=user).values("match_id")).count())


def _has_league(user) -> bool:
    return user.friend_league_memberships.exists()


def _club(user):
    from users.models import Follow

    follow = Follow.objects.filter(user=user, team__isnull=False).select_related("team").first()
    return follow.team if follow else None


def _open_poll(user, kind: str) -> int:
    from engagement.polls import active_polls

    return int(any(p.kind == kind and not p.votes.filter(user=user).exists() for p in active_polls()))


def _followed_players(user) -> int:
    from users.models import Follow

    return Follow.objects.filter(user=user, player__isnull=False).count()


# Приоритетные (двигают рейтинги и свежий контент) идут первыми, остальные — случайно.
PRIORITY_KEYS = ("evaluate", "episode_vote", "duel_vote")
POOL = (
    QuestDef("evaluate", "Оцените матч", "ti-star", 20, lambda u: min(1, _votable_count(u)), "matches:list"),
    QuestDef("predict", "Сделайте прогнозы на матчи", "ti-crystal-ball", 15, _predictable_count, "matches:list"),
    QuestDef("react", "Поставьте реакцию на матчи", "ti-mood-happy", 10, _react_count, "matches:list"),
    QuestDef("league", "Проверьте таблицу своей лиги с друзьями", "ti-users-group", 5, lambda u: int(_has_league(u)), "engagement:friend_leagues"),
    QuestDef("create_league", "Создайте лигу прогнозистов с друзьями", "ti-trophy", 15, lambda u: int(not _has_league(u)), "engagement:friend_leagues"),
    QuestDef("fan_zone", "Загляните в фан-зону своего клуба", "ti-heart", 5, lambda u: int(_club(u) is not None), ""),
    QuestDef("round", "Посмотрите «Лучших тура»", "ti-calendar-star", 5, lambda u: 1, "round_squad:round"),
    QuestDef("episode_vote", "Рассудите спорный момент тура", "ti-scale", 10, lambda u: _open_poll(u, "episode"), "core:home"),
    QuestDef("duel_vote", "Дуэль тура: кто сыграл сильнее?", "ti-swords", 10, lambda u: _open_poll(u, "duel"), "core:home"),
    QuestDef("share", "Поделитесь карточкой или ссылкой с другом", "ti-share-3", 10, lambda u: 1, "engagement:invite"),
    QuestDef("follow_player", "Подпишитесь на игрока", "ti-user-plus", 5, lambda u: int(_followed_players(u) < 5), "players:list"),
    QuestDef("leaderboard", "Проверьте своё место в рейтинге болельщиков", "ti-chart-bar", 5, lambda u: 1, "users:leaderboard"),
    QuestDef("season_pass", "Загляните в сезонный пропуск", "ti-ticket", 5, lambda u: 1, "engagement:season_pass"),
    QuestDef("player_page", "Изучите профиль игрока", "ti-id", 5, lambda u: 1, "players:list"),
)
POOL_BY_KEY = {q.key: q for q in POOL}


def _ensure(user, today):
    """Создать задания дня, если их ещё нет."""
    from engagement.models import DailyQuest

    existing = list(DailyQuest.objects.filter(user=user, date=today))
    if existing:
        return existing
    # Seed по пользователю и дате: набор стабилен в течение дня, но меняется день ото дня.
    rng = random.Random(f"{user.pk}:{today.isoformat()}")
    rest = [q for q in POOL if q.key not in PRIORITY_KEYS]
    rng.shuffle(rest)
    ordered = [POOL_BY_KEY[k] for k in PRIORITY_KEYS] + rest
    chosen = []
    for quest in ordered:
        target = quest.target(user)
        if target > 0:
            chosen.append(DailyQuest(user=user, date=today, key=quest.key, target=target, xp_reward=quest.xp))
        if len(chosen) == QUESTS_PER_DAY:
            break
    try:
        with transaction.atomic():
            DailyQuest.objects.bulk_create(chosen)
    except IntegrityError:  # параллельный запрос успел раньше
        pass
    return list(DailyQuest.objects.filter(user=user, date=today))


def today_quests(user) -> dict:
    """Задания дня для панели: список, сколько выполнено, бонус за все."""
    from django.urls import reverse

    today = timezone.localdate()
    rows = [q for q in _ensure(user, today) if q.key != ALL_DONE_KEY]
    items = []
    club = _club(user)
    for q in rows:
        definition = POOL_BY_KEY.get(q.key)
        if not definition:
            continue
        if q.key == "fan_zone" and club:
            url = reverse("teams:detail", args=[club.pk]) + "#fan-zone"
        elif definition.url_name == "matches:list" and q.key == "evaluate":
            url = reverse("matches:list") + "?status=votable"
        elif q.key in ("episode_vote", "duel_vote"):
            url = reverse("core:home") + "#polls"
        elif definition.url_name == "matches:list" and q.key == "predict":
            url = reverse("matches:list") + "?status=scheduled"
        else:
            url = reverse(definition.url_name) if definition.url_name else ""
        items.append({
            "key": q.key, "title": definition.title, "icon": definition.icon, "xp": q.xp_reward,
            "progress": min(q.progress, q.target), "target": q.target, "done": q.is_done,
            "percent": round(min(q.progress, q.target) * 100 / q.target) if q.target else 100, "url": url,
        })
    done = sum(1 for i in items if i["done"])
    return {"items": items, "done": done, "total": len(items), "all_done": bool(items) and done == len(items), "bonus_xp": ALL_DONE_XP}


def track(user, key: str, amount: int = 1) -> None:
    """Засчитать действие в задание дня (если такое задание сегодня есть)."""
    from engagement.models import DailyQuest
    from engagement.rewards import award_xp

    if not getattr(user, "is_authenticated", False):
        return
    today = timezone.localdate()
    _ensure(user, today)
    with transaction.atomic():
        quest = DailyQuest.objects.select_for_update().filter(
            user=user, date=today, key=key, completed_at__isnull=True,
        ).first()
        if not quest:
            return
        quest.progress = min(quest.target, quest.progress + amount)
        if quest.progress >= quest.target:
            quest.completed_at = timezone.now()
        quest.save(update_fields=["progress", "completed_at", "updated_at"])
    if quest.completed_at:
        award_xp(user, quest.xp_reward, f"задание {key}")
        _maybe_all_done(user, today)


def _maybe_all_done(user, today) -> None:
    from engagement.models import DailyQuest
    from engagement.rewards import award_xp

    quests = DailyQuest.objects.filter(user=user, date=today).exclude(key=ALL_DONE_KEY)
    if quests.exists() and not quests.filter(completed_at__isnull=True).exists():
        _, created = DailyQuest.objects.get_or_create(
            user=user, date=today, key=ALL_DONE_KEY,
            defaults={"target": 1, "progress": 1, "xp_reward": ALL_DONE_XP, "completed_at": timezone.now()},
        )
        if created:
            award_xp(user, ALL_DONE_XP, "все задания дня")
