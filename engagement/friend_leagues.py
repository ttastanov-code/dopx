# engagement/friend_leagues.py
"""Лиги прогнозистов с друзьями: 1 очко за угаданный исход матча сезона,
сыгранного после создания лиги. Таблица считается на лету."""
from __future__ import annotations

from django.db.models import Count, Q

MAX_MEMBERS = 50
MAX_LEAGUES_PER_USER = 10


class LeagueError(Exception):
    pass


def create(owner, name: str):
    from engagement.models import FriendLeague, FriendLeagueMember
    from engagement.season import current_season

    season = current_season()
    if season is None:
        raise LeagueError("Сейчас нет активного сезона.")
    if owner.friend_league_memberships.count() >= MAX_LEAGUES_PER_USER:
        raise LeagueError(f"Можно состоять максимум в {MAX_LEAGUES_PER_USER} лигах.")
    league = FriendLeague.objects.create(name=name.strip()[:60] or "Лига друзей", owner=owner, season=season)
    FriendLeagueMember.objects.create(league=league, user=owner)
    return league


def join(user, league) -> bool:
    """True — вступил сейчас; False — уже был участником."""
    from engagement.models import FriendLeagueMember

    if league.memberships.filter(user=user).exists():
        return False
    if league.memberships.count() >= MAX_MEMBERS:
        raise LeagueError("В лиге уже максимум участников.")
    if user.friend_league_memberships.count() >= MAX_LEAGUES_PER_USER:
        raise LeagueError(f"Можно состоять максимум в {MAX_LEAGUES_PER_USER} лигах.")
    FriendLeagueMember.objects.create(league=league, user=user)
    from engagement.notify import league_joined
    league_joined(league, user)
    return True


def standings(league) -> list[dict]:
    """Участники по очкам: угадано, всего решённых прогнозов, точность."""
    from predictions.models import MatchPrediction
    from predictions.services import correct_prediction_q

    decided = MatchPrediction.objects.filter(
        user__friend_league_memberships__league=league,
        match__season=league.season, match__status="finished",
        match__home_score__isnull=False, match__away_score__isnull=False,
    ).filter(
        # Без end_time — по времени старта.
        Q(match__end_time__gte=league.created_at)
        | Q(match__end_time__isnull=True, match__start_time__gte=league.created_at)
    )
    correct_q = correct_prediction_q()
    stats = {
        row["user_id"]: row for row in decided.values("user_id").annotate(total=Count("id"), points=Count("id", filter=correct_q))
    }
    rows = []
    for member in league.memberships.select_related("user"):
        row = stats.get(member.user_id, {"total": 0, "points": 0})
        rows.append({
            "user": member.user, "points": row["points"], "total": row["total"],
            "accuracy": round(row["points"] * 100 / row["total"]) if row["total"] else None,
            "joined_at": member.created_at,
        })
    rows.sort(key=lambda r: (-r["points"], -(r["accuracy"] or 0), r["joined_at"]))
    for place, row in enumerate(rows, start=1):
        row["place"] = place
    return rows
