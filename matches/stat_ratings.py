# matches/stat_ratings.py
"""Оценка игрока «по статистике» (raw["RATING"] из статистики матча, шкала 1-10).
На сайте показывается рядом с оценкой болельщиков.
"""
from __future__ import annotations

from statistics import mean

from matches.models import MatchPlayerStatistics


def _to_rating(raw) -> float | None:
    value = (raw or {}).get("RATING")
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def stat_ratings_for_match(match) -> dict:
    """{player_id: рейтинг} по одному матчу — один запрос."""
    result = {}
    for player_id, raw in MatchPlayerStatistics.objects.filter(match=match).values_list("player_id", "raw"):
        rating = _to_rating(raw)
        if rating is not None:
            result[player_id] = rating
    return result


def stat_ratings_for_player(player, match_ids=None) -> dict:
    """{match_id: рейтинг} по игроку (опционально — только по указанным матчам)."""
    qs = MatchPlayerStatistics.objects.filter(player=player)
    if match_ids is not None:
        qs = qs.filter(match_id__in=list(match_ids))
    result = {}
    for match_id, raw in qs.values_list("match_id", "raw"):
        rating = _to_rating(raw)
        if rating is not None:
            result[match_id] = rating
    return result


def average_stat_rating(player, match_ids=None) -> tuple[float | None, int]:
    """(средний рейтинг по статистике, число матчей с рейтингом)."""
    ratings = list(stat_ratings_for_player(player, match_ids).values())
    if not ratings:
        return None, 0
    return round(mean(ratings), 2), len(ratings)
