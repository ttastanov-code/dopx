# aggregates/tasks.py
"""
Celery-задачи модуля aggregates.

КЛЮЧЕВОЕ ИЗМЕНЕНИЕ (аудит производительности БД):
--------------------------------------------------
`recalculate_player_aggregates` и `recalculate_coach_aggregates` раньше
дергали `Model.objects.update_or_create(...)` внутри Python-цикла по каждому
игроку/тренеру матча. Каждый вызов `update_or_create` — это МИНИМУМ 2 запроса
к БД (SELECT для проверки существования + INSERT либо UPDATE), а при гонках
(race condition) между параллельными воркерами Celery — потенциально 3
(SELECT, неудачный INSERT из-за UniqueViolation, повторный UPDATE).

Для матча с 22+ игроками это давало ДО 30+ последовательных запросов на
КАЖДЫЙ вызов задачи, а задача триггерится на каждое сохранение оценки
(см. aggregates/signals.py). При активном голосовании после матча это прямой
путь к деградации пула соединений PostgreSQL.

РЕШЕНИЕ: одна атомарная batch-операция `bulk_create(..., update_conflicts=True)`
поверх уникального ограничения `unique_player_match_aggregate` на
`(player, match)`. PostgreSQL сам решает "вставить или обновить" на уровне
`INSERT ... ON CONFLICT (...) DO UPDATE SET ...` — это ОДИН SQL-запрос
независимо от количества игроков в матче.
"""
from __future__ import annotations

import logging
import math
import uuid
from datetime import timedelta
from typing import Iterable

from celery import shared_task
from django.core.cache import cache
from django.db import transaction
from django.db.models import Count, F, Q, Sum
from django.utils import timezone

from aggregates.models import CoachMatchAggregate, MatchAggregate, PlayerMatchAggregate
from evaluations.models import CoachEvaluation, ContextEvaluation, MatchEvaluation, PlayerEvaluation
from matches.models import Match
from seasons.models import Season
from teams.models import Team, TeamSeasonStats
from users.models import User

logger = logging.getLogger(__name__)

# Поля, которые пересчитываются при КОНФЛИКТЕ (т.е. запись уже существовала).
# 'id' и 'created_at' сюда намеренно НЕ включены — Postgres сохранит
# оригинальные значения существующей строки автоматически.
PLAYER_AGGREGATE_UPDATE_FIELDS: tuple[str, ...] = (
    "avg_contribution",
    "avg_risk",
    "avg_potential",
    "total_votes",
    "performance_score",
    "risk_index",
    "maturity_score",
    "stability_index",
    "clutch_index",
    "updated_at",
)

COACH_AGGREGATE_UPDATE_FIELDS: tuple[str, ...] = (
    "avg_tactics",
    "avg_substitutions",
    "avg_management",
    "avg_impact",
    "total_votes",
    "updated_at",
)


def calculate_user_weight(user: User, context_eval: ContextEvaluation | None) -> float:
    """
    Расчёт веса голоса пользователя.

    Формула:
        - 1.0 базовый вес;
        - +0.2, если пользователь смотрел матч полностью (`watched_type == 'full'`);
        - +0.2, если `trust_score` пользователя выше 1.2;
        - итоговый вес ограничен снизу значением 0.5.
    """
    weight = 1.0
    if context_eval and context_eval.watched_type == "full":
        weight += 0.2
    if user.trust_score > 1.2:
        weight += 0.2
    return max(0.5, weight)


def calculate_std_dev(values: Iterable[float]) -> float:
    """Стандартное отклонение выборки. Возвращает 0.0, если данных меньше двух точек."""
    values = list(values)
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((x - mean) ** 2 for x in values) / len(values)
    return math.sqrt(variance)


@shared_task(bind=True, max_retries=3, rate_limit="10/m")
def recalculate_player_aggregates(self, match_id: str) -> bool:
    """
    Пересчитывает агрегированные показатели ВСЕХ игроков матча.

    ДО РЕФАКТОРИНГА: N игроков → до 2*N запросов (update_or_create в цикле).
    ПОСЛЕ РЕФАКТОРИНГА: 1 запрос на выборку оценок + 1 batch-upsert запрос,
    независимо от количества игроков.

    :param match_id: UUID матча строкой (Celery не умеет сериализовать UUID напрямую).
    :return: True при успехе, False — если матч/оценки не найдены или match_id невалиден.
    """
    try:
        match_uuid = uuid.UUID(match_id)
    except (ValueError, AttributeError, TypeError):
        logger.error("Invalid match_id passed to recalculate_player_aggregates: %r", match_id)
        return False

    match = Match.objects.filter(id=match_uuid).only("id", "status").first()
    if not match:
        logger.error("Match not found: %s", match_id)
        return False

    logger.info("Starting player aggregate recalculation for match %s", match_id)

    evaluations = PlayerEvaluation.objects.filter(match_id=match_uuid).select_related(
        "user", "player"
    ).only("user_id", "player_id", "contribution", "risk", "potential")

    # Группируем оценки по игроку одним проходом по уже загруженному в память
    # QuerySet (list() форсирует один SQL-запрос вместо N).
    player_eval_map: dict[uuid.UUID, list[PlayerEvaluation]] = {}
    for eval_obj in evaluations:
        player_eval_map.setdefault(eval_obj.player_id, []).append(eval_obj)

    if not player_eval_map:
        logger.info("No player evaluations for match %s", match_id)
        return True

    # drama_index читается один раз для всего матча (а не на каждой итерации цикла,
    # как было раньше) — экономим лишние обращения к кэшу.
    drama_index = _get_match_drama_index(match_id, match_uuid)

    now = timezone.now()
    aggregates_to_upsert: list[PlayerMatchAggregate] = []

    for player_id, player_evals in player_eval_map.items():
        contributions = [e.contribution for e in player_evals]
        risks = [e.risk for e in player_evals]
        potentials = [e.potential for e in player_evals]

        avg_contribution = sum(contributions) / len(contributions)
        avg_risk = sum(risks) / len(risks)
        avg_potential = sum(potentials) / len(potentials)

        std_dev = calculate_std_dev(contributions)
        stability_index = 1.0 / std_dev if std_dev > 0 else 10.0
        clutch_index = avg_contribution * (drama_index / 10.0)

        aggregates_to_upsert.append(
            PlayerMatchAggregate(
                # id генерируется клиентски (uuid4) — используется ТОЛЬКО если
                # строки для (player, match) ещё не существует. При конфликте
                # Postgres сохраняет id уже существующей строки без изменений.
                id=uuid.uuid4(),
                player_id=player_id,
                match_id=match_uuid,
                avg_contribution=round(avg_contribution, 2),
                avg_risk=round(avg_risk, 2),
                avg_potential=round(avg_potential, 2),
                total_votes=len(player_evals),
                performance_score=round(avg_contribution, 2),
                risk_index=round(avg_risk, 2),
                maturity_score=round(avg_contribution - avg_risk, 2),
                stability_index=round(stability_index, 2),
                clutch_index=round(clutch_index, 2),
                # auto_now_add/auto_now НЕ применяются в bulk_create — выставляем вручную.
                created_at=now,
                updated_at=now,
            )
        )

    with transaction.atomic():
        PlayerMatchAggregate.objects.bulk_create(
            aggregates_to_upsert,
            update_conflicts=True,
            unique_fields=["player", "match"],
            update_fields=PLAYER_AGGREGATE_UPDATE_FIELDS,
            batch_size=500,
        )

    cache.delete(f"match_player_aggregates_{match_id}")

    logger.info(
        "Upserted %d player aggregates for match %s in a single batch query",
        len(aggregates_to_upsert),
        match_id,
    )
    return True


def _get_match_drama_index(match_id: str, match_uuid: uuid.UUID) -> float:
    """Достаёт drama_index матча из кэша, при промахе — из БД (один раз на пересчёт)."""
    cached = cache.get(f"match_aggregate_{match_id}")
    if cached:
        return cached.get("drama_index", 5.0)
    match_agg = MatchAggregate.objects.filter(match_id=match_uuid).only("drama_index").first()
    return match_agg.drama_index if match_agg else 5.0


@shared_task(bind=True, max_retries=3)
def recalculate_coach_aggregates(self, match_id: str) -> bool:
    """
    Пересчитывает агрегаты тренеров матча тем же batch-upsert подходом,
    что и `recalculate_player_aggregates` — по аналогичным соображениям
    производительности (тренеров в матче меньше, чем игроков, но принцип
    "1 SQL-запрос вместо N" остаётся верным).
    """
    try:
        match_uuid = uuid.UUID(match_id)
    except (ValueError, AttributeError, TypeError):
        logger.error("Invalid match_id passed to recalculate_coach_aggregates: %r", match_id)
        return False

    match = Match.objects.filter(id=match_uuid).only("id").first()
    if not match:
        return False

    evaluations = CoachEvaluation.objects.filter(match_id=match_uuid).select_related(
        "user", "coach"
    ).only("user_id", "coach_id", "tactics", "substitutions", "game_management", "impact")

    coach_eval_map: dict[uuid.UUID, list[CoachEvaluation]] = {}
    for eval_obj in evaluations:
        coach_eval_map.setdefault(eval_obj.coach_id, []).append(eval_obj)

    if not coach_eval_map:
        return True

    now = timezone.now()
    aggregates_to_upsert: list[CoachMatchAggregate] = []

    for coach_id, coach_evals in coach_eval_map.items():
        total = len(coach_evals)
        aggregates_to_upsert.append(
            CoachMatchAggregate(
                id=uuid.uuid4(),
                coach_id=coach_id,
                match_id=match_uuid,
                avg_tactics=round(sum(e.tactics for e in coach_evals) / total, 2),
                avg_substitutions=round(sum(e.substitutions for e in coach_evals) / total, 2),
                avg_management=round(sum(e.game_management for e in coach_evals) / total, 2),
                avg_impact=round(sum(e.impact for e in coach_evals) / total, 2),
                total_votes=total,
                created_at=now,
                updated_at=now,
            )
        )

    with transaction.atomic():
        CoachMatchAggregate.objects.bulk_create(
            aggregates_to_upsert,
            update_conflicts=True,
            unique_fields=["coach", "match"],
            update_fields=COACH_AGGREGATE_UPDATE_FIELDS,
            batch_size=500,
        )

    for coach_id in coach_eval_map:
        cache.delete(f"coach_aggregate_{coach_id}_{match_id}")

    return True


@shared_task(bind=True, max_retries=3)
def recalculate_match_aggregate(self, match_id: str) -> bool:
    """Пересчёт единственного (OneToOne) агрегата матча."""
    try:
        match_uuid = uuid.UUID(match_id)
    except (ValueError, AttributeError, TypeError):
        logger.error("Invalid match_id passed to recalculate_match_aggregate: %r", match_id)
        return False

    match = Match.objects.filter(id=match_uuid).only("id").first()
    if not match:
        return False

    evaluations = MatchEvaluation.objects.filter(match_id=match_uuid).select_related(
        "user"
    ).only("user_id", "entertainment", "tension", "fairness", "turning_point")

    eval_list = list(evaluations)

    if not eval_list:
        MatchAggregate.objects.update_or_create(
            match_id=match_uuid,
            defaults={
                "avg_entertainment": 0.0,
                "avg_tension": 0.0,
                "avg_fairness": 0.0,
                "turning_point_ratio": 0.0,
                "total_votes": 0,
                "drama_index": 0.0,
            },
        )
        return True

    avg_entertainment = sum(e.entertainment for e in eval_list) / len(eval_list)
    avg_tension = sum(e.tension for e in eval_list) / len(eval_list)
    avg_fairness = sum(e.fairness for e in eval_list) / len(eval_list)
    turning_point_ratio = sum(1 for e in eval_list if e.turning_point) / len(eval_list)
    drama_index = avg_entertainment * avg_tension

    MatchAggregate.objects.update_or_create(
        match_id=match_uuid,
        defaults={
            "avg_entertainment": round(avg_entertainment, 2),
            "avg_tension": round(avg_tension, 2),
            "avg_fairness": round(avg_fairness, 2),
            "turning_point_ratio": round(turning_point_ratio, 2),
            "total_votes": len(eval_list),
            "drama_index": round(drama_index, 2),
        },
    )

    cache.set(
        f"match_aggregate_{match_id}",
        {
            "drama_index": drama_index,
            "avg_entertainment": avg_entertainment,
            "avg_tension": avg_tension,
        },
        timeout=600,
    )

    recalculate_player_aggregates.delay(match_id)
    return True


@shared_task(bind=True, max_retries=3)
def recalculate_all_aggregates_for_match(self, match_id: str) -> bool:
    """Полный пересчёт всех агрегатов матча. Порядок: Match -> Coach -> Player."""
    logger.info("Starting full aggregate recalculation for match %s", match_id)
    recalculate_match_aggregate.delay(match_id)
    recalculate_coach_aggregates.delay(match_id)
    logger.info("Queued aggregate recalculation tasks for match %s", match_id)
    return True


@shared_task
def recalculate_all_aggregates() -> int:
    """Периодический пересчёт агрегатов для всех недавно активных матчей."""
    logger.info("Starting periodic aggregate recalculation")
    now = timezone.now()

    active_match_ids = list(
        Match.objects.filter(voting_open_until__gte=now - timedelta(hours=24))
        .only("id")
        .values_list("id", flat=True)
    )

    if not active_match_ids:
        logger.info("No active matches for aggregate recalculation")
        return 0

    for match_id in active_match_ids:
        recalculate_all_aggregates_for_match.delay(str(match_id))

    logger.info("Queued %d match aggregate recalculation tasks", len(active_match_ids))
    return len(active_match_ids)


@shared_task
def cleanup_old_sessions() -> bool:
    """Точка расширения для очистки устаревших кэшей/сессий."""
    logger.info("Running cleanup task")
    return True


@shared_task(bind=True, max_retries=3)
def trigger_aggregate_recalculation(self, match_id: str) -> bool:
    """Триггер пересчёта агрегатов, используется из aggregates/signals.py."""
    try:
        recalculate_all_aggregates_for_match.delay(match_id)
        logger.info("Triggered aggregate recalculation for match %s", match_id)
        return True
    except Exception as exc:
        logger.error("Error triggering recalculation: %s", exc)
        raise self.retry(exc=exc, countdown=60)


@shared_task
def recalculate_season_standings(season_id: int | None = None) -> dict:
    """Пересчёт турнирной таблицы сезона (без изменений в логике — уже оптимизировано)."""
    if season_id is None:
        season = Season.objects.filter(is_active=True).first()
        if not season:
            logger.warning("No active season found for standings recalculation")
            return {"success": False, "error": "No active season"}
        season_id = season.id
        logger.info("Auto-detected active season: %s", season_id)
    else:
        try:
            season = Season.objects.get(id=season_id)
        except Season.DoesNotExist:
            logger.error("Season %s not found", season_id)
            return {"success": False, "error": "Season not found"}

    teams = Team.objects.filter(teamseason__season=season, is_active=True)

    with transaction.atomic():
        for team in teams:
            stats = Match.objects.filter(season=season, status="finished").aggregate(
                played=Count("id", filter=Q(home_team=team) | Q(away_team=team)),
                wins=Count(
                    "id",
                    filter=(
                        (Q(home_team=team) & Q(home_score__gt=F("away_score")))
                        | (Q(away_team=team) & Q(away_score__gt=F("home_score")))
                    ),
                ),
                draws=Count(
                    "id",
                    filter=(
                        (Q(home_team=team) & Q(home_score=F("away_score")))
                        | (Q(away_team=team) & Q(away_score=F("home_score")))
                    ),
                ),
                goals_scored=Sum(F("home_score"), filter=Q(home_team=team))
                + Sum(F("away_score"), filter=Q(away_team=team)),
                goals_conceded=Sum(F("away_score"), filter=Q(home_team=team))
                + Sum(F("home_score"), filter=Q(away_team=team)),
            )

            played = stats["played"] or 0
            wins = stats["wins"] or 0
            draws = stats["draws"] or 0
            losses = played - wins - draws
            goals_scored = stats["goals_scored"] or 0
            goals_conceded = stats["goals_conceded"] or 0

            TeamSeasonStats.objects.update_or_create(
                team=team,
                season=season,
                defaults={
                    "played": played,
                    "wins": wins,
                    "draws": draws,
                    "losses": losses,
                    "goals_scored": goals_scored,
                    "goals_conceded": goals_conceded,
                    "goal_diff": goals_scored - goals_conceded,
                    "points": wins * 3 + draws,
                },
            )

        standings = TeamSeasonStats.objects.filter(season=season).order_by(
            "-points", "-goal_diff", "-goals_scored"
        )
        for position, stat in enumerate(standings, start=1):
            stat.position = position
            stat.save(update_fields=["position"])

    logger.info("Standings recalculated for season %s: %d teams", season_id, teams.count())
    return {"success": True, "teams": teams.count(), "season_id": season_id}