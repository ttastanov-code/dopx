# core/form_chart.py
"""Данные для компонента «Форма по матчам» (components/_form_chart.html)."""
from __future__ import annotations

from django.urls import reverse

# Сколько последних матчей показывать.
FORM_CHART_MATCHES = 10


def _short(name: str) -> str:
    """Короткое имя соперника для подписи под столбиком."""
    return name if len(name) <= 9 else name[:8] + "…"


def build_form_points(aggregates, team_id, min_votes: int, limit: int = FORM_CHART_MATCHES) -> list[dict]:
    """Точки графика по агрегатам (новые первыми на входе) — на выходе хронологически.

    :param team_id: команда, чей соперник подписывается; None — подпись «хозяева–гости».
    """
    points = []
    for agg in list(aggregates)[:limit]:
        match = agg.match
        if team_id == match.home_team_id:
            opponent = match.away_team.name
        elif team_id == match.away_team_id:
            opponent = match.home_team.name
        else:
            opponent = f"{match.home_team.name}–{match.away_team.name}"
        enough = (agg.total_votes or 0) >= min_votes
        score = agg.performance_score if enough else None
        stat = getattr(agg, "stat_rating", None)
        points.append({
            "score": score,
            "height": round(max(0.0, min(10.0, score)) * 10) if score is not None else 18,
            "tone": ("good" if score >= 7 else "mid" if score >= 5 else "low") if score is not None else "none",
            "stat": stat,
            "stat_pos": round(max(0.0, min(10.0, stat)) * 10) if stat else None,
            "opponent": _short(opponent),
            "opponent_full": opponent,
            "date": match.start_time,
            "votes": agg.total_votes or 0,
            "url": reverse("matches:detail", args=[match.id]),
        })
    points.reverse()
    return points
