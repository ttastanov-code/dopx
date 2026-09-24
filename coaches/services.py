# coaches/services.py
"""Актуальность тренеров: текущий тренер команды — тот, кто был на её последнем сыгранном матче.
Остальные тренеры с этой командой -> is_active=False (team не обнуляем).
"""
from __future__ import annotations

import logging

from django.db.models import Q

from teams.models import Team

logger = logging.getLogger(__name__)


def refresh_coach_activity() -> dict:
    """Пересчёт is_active тренеров. Идемпотентно.
    Возвращает {"teams_checked", "deactivated", "reactivated"}.
    """
    from coaches.models import Coach
    from matches.models import Match

    teams_checked = 0
    deactivated = 0
    reactivated = 0

    # Только команды с тренерами.
    team_ids = set(
        Coach.objects.filter(team__isnull=False).values_list("team_id", flat=True).distinct()
    )

    for team in Team.objects.filter(id__in=team_ids):
        last_match = (
            Match.objects.filter(status="finished")
            .filter(Q(home_team=team) | Q(away_team=team))
            .order_by("-start_time")
            .only("id", "home_team_id", "away_team_id", "home_coach_id", "away_coach_id")
            .first()
        )
        if last_match is None:
            continue
        teams_checked += 1

        current_coach_id = (
            last_match.home_coach_id if last_match.home_team_id == team.id else last_match.away_coach_id
        )

        team_coaches = list(Coach.objects.filter(team_id=team.id))
        for coach in team_coaches:
            if coach.id == current_coach_id:
                if not coach.is_active:
                    coach.is_active = True
                    coach.save(update_fields=["is_active", "updated_at"])
                    reactivated += 1
                continue
            if coach.is_active:
                coach.is_active = False
                coach.save(update_fields=["is_active", "updated_at"])
                deactivated += 1
                logger.info(
                    "Sportmonks: тренер %s больше не ведёт %s (последний матч команды — другой тренер), помечен неактивным",
                    coach, team,
                )

    return {"teams_checked": teams_checked, "deactivated": deactivated, "reactivated": reactivated}
