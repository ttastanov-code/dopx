# coaches/services.py
"""
Гигиена данных тренеров — источник (Sportmonks) не сообщает "тренер X больше
не работает в команде Y" явным событием. `parsers/sportmonks/importers.py::
get_or_create_coach` синкает `Coach.team`/`is_active=True` ТОЛЬКО когда
тренер реально встретился в свежем fixture — а если тренера уволили и он
просто перестал появляться в новых матчах, его запись в БД замирает
навсегда на последнем известном состоянии: "команда X, активен" (см.
жалобу пользователя 2026-09-09 — Кержаков в Кайрате показывался активным
спустя долгое время после ухода).

`refresh_coach_activity()` не делает новых запросов к API — считает
"реального текущего тренера" команды из уже импортированных матчей: тренер
на последнем СЫГРАННОМ матче команды. Любой другой тренер, у которого
`team` указывает на эту же команду, помечается `is_active=False` (поле
`team` НЕ обнуляем — это единственный явный факт "где он последний раз
точно работал", тот же принцип, что и Player.team у ушедших игроков).
"""
from __future__ import annotations

import logging

from django.db.models import Q

from teams.models import Team

logger = logging.getLogger(__name__)


def refresh_coach_activity() -> dict:
    """Пересчитывает is_active у всех тренеров, привязанных к командам с
    хотя бы одним сыгранным матчем. Безопасно гонять сколько угодно раз —
    идемпотентно, ничего не удаляет, только выставляет is_active/updated_at.
    Возвращает {"teams_checked", "deactivated", "reactivated"} для лога/аудита."""
    from coaches.models import Coach
    from matches.models import Match

    teams_checked = 0
    deactivated = 0
    reactivated = 0

    # Только команды, у которых вообще есть привязанные тренеры — не гонять
    # запрос на каждую команду в базе впустую.
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
