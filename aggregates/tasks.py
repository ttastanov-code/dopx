# aggregates/tasks.py
"""Celery-задачи пересчёта агрегатов и антифрод-детекторов.

Формула агрегата (вес голоса, винзоризация, сегментация свои/чужие/нейтральные)
живёт в aggregates/services.py, здесь — batch-upsert вокруг неё: один запрос
на весь матч. Детекторы: всплески крайних оценок (vote_spike) и расхождение
оценок со статистикой матча (игроки, команды, тренеры).
"""
from __future__ import annotations

import logging
import statistics
import uuid
from collections import defaultdict
from datetime import timedelta

from celery import shared_task
from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from django.db import transaction
from django.db.models import Count, F, Q, Sum
from django.utils import timezone

from aggregates.models import (
    CoachMatchAggregate,
    MatchAggregate,
    PlayerMatchAggregate,
    PlayerRatingCorrection,
    RefereeMatchAggregate,
    TeamMatchAggregate,
    TeamRatingCorrection,
)
from aggregates.services import (
    USER_FLAG_SOURCES,
    apply_neutral_anchor,
    build_allegiance,
    build_user_weight_map,
    calculate_std_dev,
    calculate_weighted_average,
    coach_team_for_match,
    countable_evaluations,
    min_votes_for_display,
    player_team_map_for_match,
    segment_evaluations_by_side,
    segment_evaluations_by_side_multi,
    stability_index_for,
)
from evaluations.models import (
    CoachEvaluation,
    ContextEvaluation,
    MatchEvaluation,
    PlayerEvaluation,
    RefereeEvaluation,
    TeamEvaluation,
)
from evaluations.turning_points import top_turning_points
from events.models import MatchEvent
from matches.models import Match, MatchPlayerStatistics, MatchTeamStatistics
from players.models import Player
from seasons.models import Season
from teams.models import Team, TeamSeasonStats

logger = logging.getLogger(__name__)

# Поля, обновляемые при конфликте upsert (id и created_at сохраняются).
PLAYER_AGGREGATE_UPDATE_FIELDS: tuple[str, ...] = (
    "avg_contribution",
    "avg_risk",
    "avg_potential",
    "total_votes",
    "performance_score",
    "rating_correction_applied",
    "risk_index",
    "maturity_score",
    "stability_index",
    "clutch_index",
    "own_fans_avg",
    "rival_fans_avg",
    "neutral_avg",
    "updated_at",
)

COACH_AGGREGATE_UPDATE_FIELDS: tuple[str, ...] = (
    "avg_tactics",
    "avg_substitutions",
    "avg_management",
    "avg_impact",
    "total_votes",
    "own_fans_avg",
    "rival_fans_avg",
    "neutral_avg",
    "updated_at",
)

TEAM_AGGREGATE_UPDATE_FIELDS: tuple[str, ...] = (
    "avg_tactics",
    "avg_effort",
    "avg_organization",
    "avg_mentality",
    "total_votes",
    "performance_score",
    "rating_correction_applied",
    "own_fans_avg",
    "rival_fans_avg",
    "neutral_avg",
    "updated_at",
)

REFEREE_AGGREGATE_UPDATE_FIELDS: tuple[str, ...] = (
    "avg_influence",
    "avg_decision_quality",
    "avg_fairness",
    "total_votes",
    "performance_score",
    "home_fans_avg",
    "away_fans_avg",
    "neutral_avg",
    "updated_at",
)


MATCH_RECALC_ONLY_FIELDS = (
    "id", "status", "start_time", "home_team_id", "away_team_id",
    "home_coach_id", "away_coach_id", "referee_id",
)


def _load_match(match_id: str, task_name: str):
    """(match_uuid, match) или (None, None), если id невалиден или матча нет."""
    try:
        match_uuid = uuid.UUID(match_id)
    except (ValueError, AttributeError, TypeError):
        logger.error("Invalid match_id passed to %s: %r", task_name, match_id)
        return None, None
    match = Match.objects.filter(id=match_uuid).only(*MATCH_RECALC_ONLY_FIELDS).first()
    if not match:
        logger.error("Match not found: %s", match_id)
        return None, None
    return match_uuid, match


# Без rate_limit: задача быстрая (десятки мс), а лимит держал очередь celery и всё, что за ней.
@shared_task(bind=True, max_retries=3, acks_late=True, reject_on_worker_lost=True)
def recalculate_player_aggregates(self, match_id: str, apply_correction: bool = True) -> bool:
    """Пересчитывает агрегаты всех игроков матча одним batch-upsert.

    :param match_id: UUID строкой.
    :param apply_correction: False — без авто-поправки (пересчёт истории).
    :return: False, если матч не найден или id невалиден.
    """
    match_uuid, match = _load_match(match_id, "recalculate_player_aggregates")
    if not match:
        return False

    logger.info("Starting player aggregate recalculation for match %s", match_id)

    evaluations = list(
        countable_evaluations(PlayerEvaluation.objects.filter(match_id=match_uuid), match_uuid)
        .select_related("user", "player")
        .only("user_id", "player_id", "contribution", "risk", "potential", "player__team_id", "user__trust_score")
    )

    # Сущности без допустимых голосов — агрегат устарел.
    stale_qs = PlayerMatchAggregate.objects.filter(match_id=match_uuid).exclude(
        player_id__in={e.player_id for e in evaluations}
    )
    stale_qs.delete()

    if not evaluations:
        cache.delete(f"match_player_aggregates_{match_id}")
        logger.info("No countable player evaluations for match %s", match_id)
        return True

    weight_map = build_user_weight_map(evaluations, match)
    allegiance = build_allegiance({e.user_id for e in evaluations}, match)
    team_by_player = player_team_map_for_match(match_uuid)

    # Группируем оценки по игроку одним проходом по списку.
    player_eval_map: dict[uuid.UUID, list[PlayerEvaluation]] = {}
    for eval_obj in evaluations:
        player_eval_map.setdefault(eval_obj.player_id, []).append(eval_obj)

    drama_index = _get_match_drama_index(match_id, match_uuid)  # вес считаем один раз на матч
    corrections = (
        dict(PlayerRatingCorrection.objects.filter(player_id__in=player_eval_map).values_list("player_id", "correction"))
        if apply_correction else {}
    )

    now = timezone.now()
    aggregates_to_upsert: list[PlayerMatchAggregate] = []

    for player_id, player_evals in player_eval_map.items():
        avg_contribution = calculate_weighted_average(player_evals, "contribution", weight_map)
        avg_risk = calculate_weighted_average(player_evals, "risk", weight_map)
        avg_potential = calculate_weighted_average(player_evals, "potential", weight_map)

        # Разброс — по сырым голосам, без винзоризации.
        stability_index = stability_index_for([e.contribution for e in player_evals])

        # Команда игрока в этом матче (заявка), а не текущий клуб.
        player_team_id = team_by_player.get(player_id) or player_evals[0].player.team_id
        segments = segment_evaluations_by_side_multi(
            player_evals, ("contribution", "risk"), player_team_id, match, allegiance
        )
        own_fans_avg, rival_fans_avg, neutral_avg, own_n, rival_n, neutral_n = segments["contribution"]
        _, _, neutral_risk_avg, risk_own_n, risk_rival_n, risk_neutral_n = segments["risk"]

        # performance_score и risk_index подтянуты к нейтральному якорю; avg_* — без якоря.
        performance_score = apply_neutral_anchor(
            avg_contribution, neutral_avg, own_n, rival_n, neutral_n
        )
        risk_index_value = apply_neutral_anchor(
            avg_risk, neutral_risk_avg, risk_own_n, risk_rival_n, risk_neutral_n
        )

        # Авто-поправка от детектора расхождения; сколько реально применено — после клампа 1..10.
        player_correction = corrections.get(player_id) or 0.0
        player_correction_applied = 0.0
        if player_correction:
            corrected = max(1.0, min(10.0, performance_score + player_correction))
            player_correction_applied = corrected - performance_score
            performance_score = corrected

        # drama_index в шкале 0..100, поэтому делим на 100.
        clutch_index = performance_score * (drama_index / 100.0)

        aggregates_to_upsert.append(
            PlayerMatchAggregate(
                id=uuid.uuid4(),
                player_id=player_id,
                match_id=match_uuid,
                avg_contribution=round(avg_contribution, 2),
                avg_risk=round(avg_risk, 2),
                avg_potential=round(avg_potential, 2),
                total_votes=len(player_evals),
                performance_score=round(performance_score, 2),
                rating_correction_applied=round(player_correction_applied, 3),
                risk_index=round(risk_index_value, 2),
                maturity_score=round(performance_score - risk_index_value, 2),
                stability_index=round(stability_index, 2),
                clutch_index=round(clutch_index, 2),
                own_fans_avg=round(own_fans_avg, 2) if own_fans_avg is not None else None,
                rival_fans_avg=round(rival_fans_avg, 2) if rival_fans_avg is not None else None,
                neutral_avg=round(neutral_avg, 2) if neutral_avg is not None else None,
                # auto_now не работает в bulk_create — ставим вручную.
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
    for player_id in player_eval_map:
        cache.delete(f"player_aggregate_{player_id}_{match_id}")

    logger.info(
        "Upserted %d player aggregates for match %s in a single batch query (weighted+winsorized)",
        len(aggregates_to_upsert),
        match_id,
    )
    return True


def _get_match_drama_index(match_id: str, match_uuid: uuid.UUID) -> float:
    """drama_index матча из кэша или БД. Нет оценок матча — 50.0, середина шкалы 0..100."""
    cached = cache.get(f"match_aggregate_{match_id}")
    if cached:
        return cached.get("drama_index", 50.0)
    match_agg = MatchAggregate.objects.filter(match_id=match_uuid).only("drama_index", "total_votes").first()
    if not match_agg or not match_agg.total_votes:
        return 50.0
    return match_agg.drama_index


@shared_task(bind=True, max_retries=3, acks_late=True, reject_on_worker_lost=True)
def recalculate_coach_aggregates(self, match_id: str) -> bool:
    """Пересчёт агрегатов тренеров матча (вес, винзоризация, сегментация)."""
    match_uuid, match = _load_match(match_id, "recalculate_coach_aggregates")
    if not match:
        return False

    evaluations = list(
        countable_evaluations(CoachEvaluation.objects.filter(match_id=match_uuid), match_uuid)
        .select_related("user", "coach")
        .only(
            "user_id", "coach_id", "tactics", "substitutions",
            "game_management", "impact", "coach__team_id", "user__trust_score",
        )
    )

    CoachMatchAggregate.objects.filter(match_id=match_uuid).exclude(
        coach_id__in={e.coach_id for e in evaluations}
    ).delete()

    if not evaluations:
        return True

    weight_map = build_user_weight_map(evaluations, match)
    allegiance = build_allegiance({e.user_id for e in evaluations}, match)

    coach_eval_map: dict[uuid.UUID, list[CoachEvaluation]] = {}
    for eval_obj in evaluations:
        coach_eval_map.setdefault(eval_obj.coach_id, []).append(eval_obj)

    now = timezone.now()
    aggregates_to_upsert: list[CoachMatchAggregate] = []

    for coach_id, coach_evals in coach_eval_map.items():
        pooled_tactics = calculate_weighted_average(coach_evals, "tactics", weight_map)
        pooled_substitutions = calculate_weighted_average(coach_evals, "substitutions", weight_map)
        pooled_management = calculate_weighted_average(coach_evals, "game_management", weight_map)
        pooled_impact = calculate_weighted_average(coach_evals, "impact", weight_map)

        # Команда тренера в этом матче (home_coach/away_coach), а не текущая.
        coach_team_id = coach_team_for_match(coach_evals[0].coach, match)
        # Номинации читают avg_* напрямую, поэтому якорим каждое поле отдельно.
        segments = segment_evaluations_by_side_multi(
            coach_evals,
            ("average_score", "tactics", "substitutions", "game_management", "impact"),
            coach_team_id,
            match,
            allegiance,
        )
        own_fans_avg, rival_fans_avg, neutral_avg, _, _, _ = segments["average_score"]

        def _anchor(pooled_value: float, field: str) -> float:
            _, _, field_neutral_avg, field_own_n, field_rival_n, field_neutral_n = segments[field]
            return apply_neutral_anchor(
                pooled_value, field_neutral_avg, field_own_n, field_rival_n, field_neutral_n
            )

        avg_tactics = _anchor(pooled_tactics, "tactics")
        avg_substitutions = _anchor(pooled_substitutions, "substitutions")
        avg_management = _anchor(pooled_management, "game_management")
        avg_impact = _anchor(pooled_impact, "impact")

        aggregates_to_upsert.append(
            CoachMatchAggregate(
                id=uuid.uuid4(),
                coach_id=coach_id,
                match_id=match_uuid,
                avg_tactics=round(avg_tactics, 2),
                avg_substitutions=round(avg_substitutions, 2),
                avg_management=round(avg_management, 2),
                avg_impact=round(avg_impact, 2),
                total_votes=len(coach_evals),
                own_fans_avg=round(own_fans_avg, 2) if own_fans_avg is not None else None,
                rival_fans_avg=round(rival_fans_avg, 2) if rival_fans_avg is not None else None,
                neutral_avg=round(neutral_avg, 2) if neutral_avg is not None else None,
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


@shared_task(bind=True, max_retries=3, acks_late=True, reject_on_worker_lost=True)
def recalculate_team_aggregates(self, match_id: str, apply_correction: bool = True) -> bool:
    """Пересчёт агрегатов команд матча.

    :param apply_correction: False — без авто-поправки (пересчёт истории).
    """
    match_uuid, match = _load_match(match_id, "recalculate_team_aggregates")
    if not match:
        return False

    evaluations = list(
        countable_evaluations(TeamEvaluation.objects.filter(match_id=match_uuid), match_uuid)
        .select_related("user", "team")
        .only("user_id", "team_id", "tactics", "effort", "organization", "mentality", "user__trust_score")
    )

    TeamMatchAggregate.objects.filter(match_id=match_uuid).exclude(
        team_id__in={e.team_id for e in evaluations}
    ).delete()

    if not evaluations:
        return True

    weight_map = build_user_weight_map(evaluations, match)
    allegiance = build_allegiance({e.user_id for e in evaluations}, match)

    team_eval_map: dict[uuid.UUID, list[TeamEvaluation]] = {}
    for eval_obj in evaluations:
        team_eval_map.setdefault(eval_obj.team_id, []).append(eval_obj)

    corrections = (
        dict(TeamRatingCorrection.objects.filter(team_id__in=team_eval_map).values_list("team_id", "correction"))
        if apply_correction else {}
    )

    now = timezone.now()
    aggregates_to_upsert: list[TeamMatchAggregate] = []

    for team_id, team_evals in team_eval_map.items():
        avg_tactics = calculate_weighted_average(team_evals, "tactics", weight_map)
        avg_effort = calculate_weighted_average(team_evals, "effort", weight_map)
        avg_organization = calculate_weighted_average(team_evals, "organization", weight_map)
        avg_mentality = calculate_weighted_average(team_evals, "mentality", weight_map)
        # Итог — среднее уже защищённых полей.
        pooled_performance_score = (avg_tactics + avg_effort + avg_organization + avg_mentality) / 4

        own_fans_avg, rival_fans_avg, neutral_avg, own_n, rival_n, neutral_n = (
            segment_evaluations_by_side(team_evals, "average_score", team_id, match, allegiance)
        )
        # Подтягиваем к нейтральному якорю при высокой доле пристрастных голосов.
        performance_score = apply_neutral_anchor(
            pooled_performance_score, neutral_avg, own_n, rival_n, neutral_n
        )

        # Авто-поправка от детектора расхождения (ограничена диапазоном 1..10).
        correction = corrections.get(team_id) or 0.0
        team_correction_applied = 0.0
        if correction:
            corrected = max(1.0, min(10.0, performance_score + correction))
            team_correction_applied = corrected - performance_score
            performance_score = corrected

        aggregates_to_upsert.append(
            TeamMatchAggregate(
                id=uuid.uuid4(),
                team_id=team_id,
                match_id=match_uuid,
                avg_tactics=round(avg_tactics, 2),
                avg_effort=round(avg_effort, 2),
                avg_organization=round(avg_organization, 2),
                avg_mentality=round(avg_mentality, 2),
                total_votes=len(team_evals),
                performance_score=round(performance_score, 2),
                rating_correction_applied=round(team_correction_applied, 3),
                own_fans_avg=round(own_fans_avg, 2) if own_fans_avg is not None else None,
                rival_fans_avg=round(rival_fans_avg, 2) if rival_fans_avg is not None else None,
                neutral_avg=round(neutral_avg, 2) if neutral_avg is not None else None,
                created_at=now,
                updated_at=now,
            )
        )

    with transaction.atomic():
        TeamMatchAggregate.objects.bulk_create(
            aggregates_to_upsert,
            update_conflicts=True,
            unique_fields=["team", "match"],
            update_fields=TEAM_AGGREGATE_UPDATE_FIELDS,
            batch_size=500,
        )

    for team_id in team_eval_map:
        cache.delete(f"team_aggregate_{team_id}_{match_id}")

    return True


def referee_performance_formula(decision_quality: float, fairness: float, influence: float) -> float:
    """0.6*decision_quality + 0.3*fairness + 0.1*(10 - influence/10). influence — шкала 0..100."""
    return 0.6 * decision_quality + 0.3 * fairness + 0.1 * (10 - influence / 10)


@shared_task(bind=True, max_retries=3, acks_late=True, reject_on_worker_lost=True)
def recalculate_referee_aggregates(self, match_id: str) -> bool:
    """Пересчёт агрегата судейства матча.

    Каждая шкала (решения, влияние, справедливость) якорится к своим нейтралам отдельно,
    потом собирается формулой referee_performance_formula.
    """
    match_uuid, match = _load_match(match_id, "recalculate_referee_aggregates")
    if not match:
        return False

    # Агрегат другого судьи (судью матча поменяли) — устарел.
    stale_qs = RefereeMatchAggregate.objects.filter(match_id=match_uuid)
    if match.referee_id:
        stale_qs = stale_qs.exclude(referee_id=match.referee_id)
    stale_qs.delete()
    if not match.referee_id:
        return True  # у матча нет судьи

    referee_evals = list(
        countable_evaluations(RefereeEvaluation.objects.filter(match_id=match_uuid), match_uuid)
        .select_related("user")
        .only("user_id", "influence_score", "decision_quality", "user__trust_score")
    )
    if not referee_evals:
        RefereeMatchAggregate.objects.filter(match_id=match_uuid).delete()
        return True

    weight_map = build_user_weight_map(referee_evals, match)

    # fairness берём из MatchEvaluation со своим weight_map.
    match_evals = list(
        countable_evaluations(MatchEvaluation.objects.filter(match_id=match_uuid), match_uuid)
        .select_related("user")
        .only("user_id", "fairness", "user__trust_score")
    )
    allegiance = build_allegiance({e.user_id for e in referee_evals} | {e.user_id for e in match_evals}, match)

    # «Свои» для судьи — болельщики хозяев.
    ref_segments = segment_evaluations_by_side_multi(
        referee_evals, ("decision_quality", "influence_score"), match.home_team_id, match, allegiance
    )

    def _anchor(pooled_value: float, segment) -> float:
        _, _, neutral_value, home_n, away_n, neutral_n = segment
        return apply_neutral_anchor(pooled_value, neutral_value, home_n, away_n, neutral_n)

    avg_influence = _anchor(
        calculate_weighted_average(referee_evals, "influence_score", weight_map), ref_segments["influence_score"]
    )
    avg_decision_quality = _anchor(
        calculate_weighted_average(referee_evals, "decision_quality", weight_map), ref_segments["decision_quality"]
    )

    if match_evals:
        fairness_weight_map = build_user_weight_map(match_evals, match)
        fairness_segment = segment_evaluations_by_side(
            match_evals, "fairness", match.home_team_id, match, allegiance
        )
        avg_fairness = _anchor(
            calculate_weighted_average(match_evals, "fairness", fairness_weight_map), fairness_segment
        )
    else:
        # Нет оценок fairness — подставляем decision_quality.
        avg_fairness = avg_decision_quality

    performance_score = referee_performance_formula(avg_decision_quality, avg_fairness, avg_influence)
    home_fans_avg, away_fans_avg, neutral_avg = ref_segments["decision_quality"][:3]

    now = timezone.now()
    RefereeMatchAggregate.objects.update_or_create(
        referee_id=match.referee_id,
        match_id=match_uuid,
        defaults={
            "avg_influence": round(avg_influence, 2),
            "avg_decision_quality": round(avg_decision_quality, 2),
            "avg_fairness": round(avg_fairness, 2),
            "total_votes": len(referee_evals),
            "performance_score": round(performance_score, 2),
            "home_fans_avg": round(home_fans_avg, 2) if home_fans_avg is not None else None,
            "away_fans_avg": round(away_fans_avg, 2) if away_fans_avg is not None else None,
            "neutral_avg": round(neutral_avg, 2) if neutral_avg is not None else None,
            "updated_at": now,
        },
    )

    cache.delete(f"referee_aggregate_{match.referee_id}_{match_id}")
    return True


@shared_task(bind=True, max_retries=3, acks_late=True, reject_on_worker_lost=True)
def recalculate_match_aggregate(self, match_id: str) -> bool:
    """Пересчёт общего агрегата матча (с весами пользователей, без сегментации)."""
    match_uuid, match = _load_match(match_id, "recalculate_match_aggregate")
    if not match:
        return False

    evaluations = list(
        countable_evaluations(MatchEvaluation.objects.filter(match_id=match_uuid), match_uuid)
        .select_related("user", "turning_point_event__player")
        .only(
            "user_id", "entertainment", "tension", "fairness", "turning_point", "turning_point_kind",
            "turning_point_event", "user__trust_score",
        )
    )

    if not evaluations:
        MatchAggregate.objects.update_or_create(
            match_id=match_uuid,
            defaults={
                "avg_entertainment": 0.0,
                "avg_tension": 0.0,
                "avg_fairness": 0.0,
                "turning_point_ratio": 0.0,
                "turning_points": [],
                "total_votes": 0,
                "drama_index": 0.0,
            },
        )
        cache.delete(f"match_aggregate_{match_id}")
        recalculate_player_aggregates.delay(match_id)
        return True

    weight_map = build_user_weight_map(evaluations, match)

    avg_entertainment = calculate_weighted_average(evaluations, "entertainment", weight_map)
    avg_tension = calculate_weighted_average(evaluations, "tension", weight_map)
    avg_fairness = calculate_weighted_average(evaluations, "fairness", weight_map)
    # turning_point — булево, считаем простой долей.
    turning_point_ratio = sum(1 for e in evaluations if e.turning_point) / len(evaluations)
    drama_index = avg_entertainment * avg_tension

    MatchAggregate.objects.update_or_create(
        match_id=match_uuid,
        defaults={
            "avg_entertainment": round(avg_entertainment, 2),
            "avg_tension": round(avg_tension, 2),
            "avg_fairness": round(avg_fairness, 2),
            "turning_point_ratio": round(turning_point_ratio, 2),
            "turning_points": top_turning_points(evaluations),
            "total_votes": len(evaluations),
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


@shared_task(bind=True, max_retries=3, acks_late=True, reject_on_worker_lost=True)
def recalculate_all_aggregates_for_match(self, match_id: str) -> bool:
    """Полный пересчёт всех агрегатов матча: Match(→Player) / Coach / Team / Referee."""
    logger.info("Starting full aggregate recalculation for match %s", match_id)
    recalculate_match_aggregate.delay(match_id)
    recalculate_coach_aggregates.delay(match_id)
    recalculate_team_aggregates.delay(match_id)
    recalculate_referee_aggregates.delay(match_id)
    logger.info("Queued aggregate recalculation tasks for match %s", match_id)
    return True


def recalculate_match_now(match_id: str) -> None:
    """Синхронный полный пересчёт одного матча (без очереди): для действий модератора и чистки."""
    match_id = str(match_id)
    recalculate_match_aggregate.run(match_id)
    recalculate_player_aggregates.run(match_id)
    recalculate_team_aggregates.run(match_id)
    recalculate_coach_aggregates.run(match_id)
    recalculate_referee_aggregates.run(match_id)


def user_evaluated_match_ids(user_id) -> set:
    """Все матчи, где у пользователя есть хоть одна оценка."""
    match_ids: set = set()
    for model in (ContextEvaluation, PlayerEvaluation, TeamEvaluation, CoachEvaluation,
                  RefereeEvaluation, MatchEvaluation):
        match_ids.update(model.objects.filter(user_id=user_id).values_list("match_id", flat=True).distinct())
    return match_ids


@shared_task
def recalculate_matches_for_user(user_id: str, match_id: str | None = None) -> int:
    """Пересчёт рейтингов после бана/разбана или решения по флагу пользователя.
    match_id — только этот матч, иначе все матчи с его оценками.
    """
    match_ids = {match_id} if match_id else user_evaluated_match_ids(user_id)
    for mid in match_ids:
        recalculate_all_aggregates_for_match.delay(str(mid))
    return len(match_ids)


def schedule_recalculation_for_flags(flags) -> None:
    """После решения модератора по флагам пользователей — пересчитать затронутые матчи."""
    for flag in flags:
        if not flag.user_id or flag.source not in USER_FLAG_SOURCES:
            continue
        transaction.on_commit(
            lambda uid=str(flag.user_id), mid=(str(flag.match_id) if flag.match_id else None):
            recalculate_matches_for_user.delay(uid, mid)
        )


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


@shared_task(bind=True, max_retries=3, acks_late=True, reject_on_worker_lost=True)
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
    """Пересчёт турнирной таблицы. Без season_id — все активные сезоны."""
    if season_id is None:
        active_seasons = list(Season.objects.filter(is_active=True))
        if not active_seasons:
            logger.warning("No active season found for standings recalculation")
            return {"success": False, "error": "No active season"}
        results = [_recalculate_standings_for_season(s) for s in active_seasons]
        return {"success": all(r["success"] for r in results), "seasons": results}

    try:
        season = Season.objects.get(id=season_id)
    except Season.DoesNotExist:
        logger.error("Season %s not found", season_id)
        return {"success": False, "error": "Season not found"}
    return _recalculate_standings_for_season(season)


def _recalculate_standings_for_season(season: Season) -> dict:
    """Пересчёт турнирной таблицы одного сезона одним запросом по всем матчам.
    Матч без счёта в таблицу не идёт (иначе засчитался бы поражением обеим командам).
    """
    season_id = season.id
    # Прошлый сезон — все участники: выбывшую/неактивную команду из истории не выкидываем.
    team_qs = Team.objects.filter(teamseason__season=season)
    if season.is_active:
        team_qs = team_qs.filter(is_active=True)
    teams = list(team_qs.distinct())
    team_ids = {team.id for team in teams}

    with transaction.atomic():
        matches_qs = Match.objects.filter(
            season=season, status="finished", home_score__isnull=False, away_score__isnull=False,
        ).values("home_team_id", "away_team_id", "home_score", "away_score")

        stats_by_team = {
            team_id: {"played": 0, "wins": 0, "draws": 0, "goals_scored": 0, "goals_conceded": 0}
            for team_id in team_ids
        }

        for m in matches_qs:
            home_score = m["home_score"]
            away_score = m["away_score"]
            for team_id, scored, conceded in (
                (m["home_team_id"], home_score, away_score),
                (m["away_team_id"], away_score, home_score),
            ):
                s = stats_by_team.get(team_id)
                if s is None:
                    continue
                s["played"] += 1
                s["goals_scored"] += scored
                s["goals_conceded"] += conceded
                if scored > conceded:
                    s["wins"] += 1
                elif scored == conceded:
                    s["draws"] += 1

        for team in teams:
            s = stats_by_team[team.id]
            played = s["played"]
            wins = s["wins"]
            draws = s["draws"]
            losses = played - wins - draws
            goals_scored = s["goals_scored"]
            goals_conceded = s["goals_conceded"]

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

    logger.info("Standings recalculated for season %s: %d teams", season_id, len(teams))
    return {"success": True, "teams": len(teams), "season_id": season_id}


# ============================================================
# Детект координированных всплесков голосования (vote_spike)
# ============================================================

# Матч активен для детекта 24 часа после закрытия голосования.
VOTE_SPIKE_LOOKBACK_HOURS = 24

# Окно поиска всплеска — организованный призыв обычно укладывается в 1-3 часа.
VOTE_SPIKE_WINDOW_HOURS = 2

# Минимум голосов в окне, чтобы доля крайних оценок что-то значила.
VOTE_SPIKE_MIN_WINDOW_VOTES = 4

# Минимум соседних сущностей матча для расчёта медианы/MAD.
VOTE_SPIKE_MIN_SIBLINGS = 5

# Порог модифицированного z-score по умолчанию (3.5, Iglewicz & Hoaglin).
# Фактический порог калибруется по решениям модераторов (users.tasks).
VOTE_SPIKE_MAD_THRESHOLD = 3.5

# Крайние оценки: 1-2 и 9-10.
EXTREME_LOW_MAX = 2
EXTREME_HIGH_MIN = 9


def _extreme_ratio(values: list[float]) -> float:
    """Доля значений на экстремальных краях шкалы 1-10."""
    if not values:
        return 0.0
    extreme = sum(1 for v in values if v <= EXTREME_LOW_MAX or v >= EXTREME_HIGH_MIN)
    return extreme / len(values)


def _modified_z_scores(values: list[float]) -> list[float]:
    """Модифицированный z-score по медиане/MAD — устойчив к выбросам. Возвращает |z|."""
    if len(values) < 2:
        return [0.0] * len(values)
    median = statistics.median(values)
    abs_deviations = [abs(v - median) for v in values]
    mad = statistics.median(abs_deviations)
    if mad == 0:
        # MAD=0 — fallback на среднее абсолютное отклонение.
        mean_abs_dev = sum(abs_deviations) / len(abs_deviations)
        if mean_abs_dev == 0:
            return [0.0] * len(values)
        return [abs(d) / (1.253314 * mean_abs_dev) for d in abs_deviations]
    return [0.6745 * d / mad for d in abs_deviations]


@shared_task
def detect_vote_velocity_anomalies_task() -> int:
    """Ищет сущности (игрок/команда/тренер) со всплеском крайних оценок в коротком окне.

    Сравнение с остальными сущностями того же матча (MAD z-score), а не с
    фиксированным порогом — так учитывается контекст матча. Рейтинг не меняет,
    только создаёт SuspiciousActivityFlag(source="vote_spike") для модератора.
    """
    from users.models import SuspiciousActivityFlag

    since_lookback = timezone.now() - timedelta(hours=VOTE_SPIKE_LOOKBACK_HOURS)
    window_start = timezone.now() - timedelta(hours=VOTE_SPIKE_WINDOW_HOURS)

    active_match_ids = list(
        Match.objects.filter(voting_open_until__gte=since_lookback)
        .only("id")
        .values_list("id", flat=True)
    )
    if not active_match_ids:
        return 0

    # Порог калибруется по решениям модераторов; читаем один раз на прогон.
    from users.tasks import ANTIFRAUD_CALIBRATED_THRESHOLDS, get_antifraud_threshold

    mad_threshold = get_antifraud_threshold(
        "vote_spike_mad_threshold", ANTIFRAUD_CALIBRATED_THRESHOLDS["vote_spike_mad_threshold"]["default"]
    )

    flagged = 0
    for match_id in active_match_ids:
        flagged += _detect_spikes_for_match(match_id, window_start, SuspiciousActivityFlag, mad_threshold)

    if flagged:
        logger.warning("Vote-velocity antifraud: flagged %d entity anomaly signal(s).", flagged)
    return flagged


def _detect_spikes_for_match(match_id, window_start, SuspiciousActivityFlag, mad_threshold: float) -> int:
    """Один матч: доля крайних оценок по каждому типу сущности отдельно, MAD-выбросы -> флаги."""
    flagged = 0

    from coaches.models import Coach
    from players.models import Player

    entity_specs = (
        (PlayerEvaluation, "player_id", "contribution", Player, "Player"),
        (TeamEvaluation, "team_id", "tactics", Team, "Team"),
        (CoachEvaluation, "coach_id", "tactics", Coach, "Coach"),
    )

    for model, id_field, value_field, entity_model, model_name in entity_specs:
        rows = model.objects.filter(match_id=match_id, created_at__gte=window_start).values_list(
            id_field, value_field
        )
        by_entity: dict = defaultdict(list)
        for entity_id, value in rows:
            by_entity[entity_id].append(value)

        eligible = {
            entity_id: values
            for entity_id, values in by_entity.items()
            if len(values) >= VOTE_SPIKE_MIN_WINDOW_VOTES
        }
        if len(eligible) < VOTE_SPIKE_MIN_SIBLINGS:
            continue  # мало соседей для сравнения

        entity_ids = list(eligible.keys())
        ratios = [_extreme_ratio(eligible[eid]) for eid in entity_ids]
        z_scores = _modified_z_scores(ratios)

        content_type = ContentType.objects.get_for_model(entity_model)

        for entity_id, ratio, z in zip(entity_ids, ratios, z_scores):
            if z < mad_threshold:
                continue

            score = round(min(1.0, z / (mad_threshold * 2)), 2)
            already_pending = SuspiciousActivityFlag.objects.filter(
                content_type=content_type,
                object_id=str(entity_id),
                match_id=match_id,
                source="vote_spike",
                status="pending",
            ).exists()
            if already_pending:
                continue

            SuspiciousActivityFlag.objects.create(
                user=None,
                content_type=content_type,
                object_id=str(entity_id),
                match_id=match_id,
                source="vote_spike",
                score=score,
                details={
                    "extreme_ratio": round(ratio, 2),
                    "modified_z_score": round(z, 2),
                    "window_votes": len(eligible[entity_id]),
                    "window_hours": VOTE_SPIKE_WINDOW_HOURS,
                    "entity_type": model_name,
                    "threshold_used": mad_threshold,
                },
            )
            flagged += 1

    return flagged


# Расхождение оценок со статистикой матчей. См. docs/adr/0009-stats-divergence-antibrigading-signal.md.

STATS_DIVERGENCE_LOOKBACK_DAYS = 120
STATS_DIVERGENCE_WINDOW_MATCHES = 8
STATS_DIVERGENCE_MIN_WINDOW_MATCHES = 5
STATS_DIVERGENCE_BASELINE_MIN_MATCHES = 8
STATS_DIVERGENCE_DOMINANCE_HIGH = 0.58
STATS_DIVERGENCE_DOMINANCE_LOW = 0.42
STATS_DIVERGENCE_RATING_Z_THRESHOLD = 0.75
STATS_DIVERGENCE_MIN_RATING_GAP = 0.5

# Поправка небольшая и затухает (DECAY раз в сутки), если паттерн не подтвердился.
STATS_DIVERGENCE_MAX_CORRECTION = 0.4
STATS_DIVERGENCE_CORRECTION_DECAY = 0.5
STATS_DIVERGENCE_CORRECTION_FLOOR = 0.02  # меньше — обнуляем

# Пауза проверки после «Отклонить» модератором.
STATS_DIVERGENCE_DISMISS_COOLDOWN_DAYS = 30

# Доля команды в игре: вес показателя — насколько он говорит о реальном преимуществе.
# Поле без данных у одной из команд пропускается.
DOMINANCE_SHARE_WEIGHTS = {
    "shots_on_goal": 2.0,
    "dangerous_attacks": 1.5,
    "shots": 1.0,
    "corners": 0.5,
    "possession_percent": 0.5,
}
DOMINANCE_SHARE_FIELDS = tuple(DOMINANCE_SHARE_WEIGHTS)


def _team_dominance_share(own_stat: MatchTeamStatistics, opponent_stat: MatchTeamStatistics) -> float | None:
    """Взвешенная доля команды в сумме показателей обеих команд по
    DOMINANCE_SHARE_WEIGHTS за один матч (0.5 — поровну). None, если ни
    по одному полю нет данных сразу у ОБЕИХ команд."""
    weighted = 0.0
    weight_sum = 0.0
    for field, weight in DOMINANCE_SHARE_WEIGHTS.items():
        own = getattr(own_stat, field, None)
        opp = getattr(opponent_stat, field, None)
        if own is None or opp is None:
            continue
        total = own + opp
        share = 0.5 if total == 0 else own / total
        weighted += share * weight
        weight_sum += weight
    if not weight_sum:
        return None
    return weighted / weight_sum


@shared_task
def detect_rating_stats_divergence_task() -> int:
    """Детектор расхождения для команд.

    Сравнивает оценки последних матчей с нормой команды и с долей команды в игре.
    Доминировала, а оценки ниже нормы — возможна накрутка против; уступала, а
    оценки выше — накрутка за. При срабатывании обновляет TeamRatingCorrection
    и флаг для модератора.
    """
    from users.models import SuspiciousActivityFlag

    since = timezone.now() - timedelta(days=STATS_DIVERGENCE_LOOKBACK_DAYS)
    active_team_ids = list(
        TeamMatchAggregate.objects.filter(match__status="finished", match__start_time__gte=since)
        .values_list("team_id", flat=True)
        .distinct()
    )
    if not active_team_ids:
        return 0

    content_type = ContentType.objects.get_for_model(Team)
    flagged = 0
    for team_id in active_team_ids:
        flagged += _check_team_stats_divergence(team_id, content_type, SuspiciousActivityFlag)

    if flagged:
        logger.warning("Stats-divergence antifraud: flagged %d team signal(s).", flagged)
    return flagged


def _raw_community_score(agg) -> float:
    """performance_score без вшитой авто-поправки — иначе поправка влияет сама на себя."""
    return agg.performance_score - (getattr(agg, "rating_correction_applied", 0.0) or 0.0)


def _sync_divergence_flag(SuspiciousActivityFlag, content_type, object_id, source, score, details) -> None:
    """Один pending-флаг на сущность, details всегда актуальные.
    Дата первого обнаружения сохраняется в first_detected_at.
    """
    now_iso = timezone.now().isoformat()
    flag = SuspiciousActivityFlag.objects.filter(
        content_type=content_type, object_id=str(object_id), source=source, status="pending",
    ).first()
    if flag is None:
        SuspiciousActivityFlag.objects.create(
            user=None, content_type=content_type, object_id=str(object_id), match=None,
            source=source, score=score,
            details={**details, "pattern_active": True, "first_detected_at": now_iso, "last_checked_at": now_iso},
        )
        return
    first_detected = (flag.details or {}).get("first_detected_at") or flag.created_at.isoformat()
    flag.details = {**details, "pattern_active": True, "first_detected_at": first_detected, "last_checked_at": now_iso}
    flag.score = score
    flag.save(update_fields=["details", "score", "updated_at"])


def _mark_divergence_flag_inactive(SuspiciousActivityFlag, content_type, object_id, source, current_correction) -> None:
    """Паттерн не подтвердился — помечаем открытый флаг неактивным с текущей поправкой."""
    if SuspiciousActivityFlag is None or content_type is None:
        return
    flag = SuspiciousActivityFlag.objects.filter(
        content_type=content_type, object_id=str(object_id), source=source, status="pending",
    ).first()
    if flag is None:
        return
    details = dict(flag.details or {})
    details["pattern_active"] = False
    details["correction_applied"] = round(current_correction, 3)
    details["last_checked_at"] = timezone.now().isoformat()
    flag.details = details
    flag.save(update_fields=["details", "updated_at"])


def _decay_team_rating_correction(team_id, content_type=None, SuspiciousActivityFlag=None) -> None:
    """Паттерн не подтвердился — поправка команды затухает."""
    correction_obj = TeamRatingCorrection.objects.filter(team_id=team_id).first()
    if correction_obj is None or correction_obj.correction == 0.0:
        _mark_divergence_flag_inactive(SuspiciousActivityFlag, content_type, team_id, "stats_divergence", 0.0)
        return
    new_value = correction_obj.correction * STATS_DIVERGENCE_CORRECTION_DECAY
    if abs(new_value) < STATS_DIVERGENCE_CORRECTION_FLOOR:
        new_value = 0.0
    correction_obj.correction = round(new_value, 3)
    correction_obj.last_pattern = ""
    correction_obj.save(update_fields=["correction", "last_pattern", "updated_at"])
    _mark_divergence_flag_inactive(SuspiciousActivityFlag, content_type, team_id, "stats_divergence", new_value)


def _check_team_stats_divergence(team_id, content_type, SuspiciousActivityFlag) -> int:
    """Одна команда: норма, окно последних матчей, доля в игре -> поправка и флаг."""
    # Модератор отклонил сигнал — пока идёт пауза, команду не трогаем.
    existing_correction = TeamRatingCorrection.objects.filter(team_id=team_id).first()
    if existing_correction and existing_correction.suppressed_until and existing_correction.suppressed_until > timezone.now():
        return 0

    fetch_limit = max(STATS_DIVERGENCE_WINDOW_MATCHES, STATS_DIVERGENCE_BASELINE_MIN_MATCHES) * 3
    aggregates = list(
        # Матчи с малым числом голосов — шум, а не сигнал.
        TeamMatchAggregate.objects.filter(
            team_id=team_id, match__status="finished", total_votes__gte=min_votes_for_display(),
        )
        .select_related("match")
        .order_by("-match__start_time")[:fetch_limit]
    )
    # Норма считается только по матчам старше окна.
    if len(aggregates) < STATS_DIVERGENCE_WINDOW_MATCHES + STATS_DIVERGENCE_BASELINE_MIN_MATCHES:
        return 0  # мало истории

    baseline_pool = aggregates[STATS_DIVERGENCE_WINDOW_MATCHES:]
    baseline_scores = [_raw_community_score(a) for a in baseline_pool]
    baseline_mean = sum(baseline_scores) / len(baseline_scores)
    baseline_std = calculate_std_dev(baseline_scores)

    window_pairs: list[tuple[float, float]] = []
    for agg in aggregates[:STATS_DIVERGENCE_WINDOW_MATCHES]:
        own_stat = MatchTeamStatistics.objects.filter(match_id=agg.match_id, team_id=team_id).first()
        if own_stat is None:
            continue
        opponent_stat = MatchTeamStatistics.objects.filter(match_id=agg.match_id).exclude(team_id=team_id).first()
        if opponent_stat is None:
            continue
        share = _team_dominance_share(own_stat, opponent_stat)
        if share is None:
            continue
        window_pairs.append((_raw_community_score(agg), share))

    if len(window_pairs) < STATS_DIVERGENCE_MIN_WINDOW_MATCHES:
        return 0  # мало матчей со статистикой

    window_rating = sum(p[0] for p in window_pairs) / len(window_pairs)
    window_dominance = sum(p[1] for p in window_pairs) / len(window_pairs)
    rating_gap = window_rating - baseline_mean

    # Порог — от собственного разброса оценок команды, но не ниже минимума.
    min_gap = max(STATS_DIVERGENCE_MIN_RATING_GAP, STATS_DIVERGENCE_RATING_Z_THRESHOLD * baseline_std)

    pattern = None
    if window_dominance >= STATS_DIVERGENCE_DOMINANCE_HIGH and rating_gap <= -min_gap:
        pattern = "underrated_despite_dominance"
    elif window_dominance <= STATS_DIVERGENCE_DOMINANCE_LOW and rating_gap >= min_gap:
        pattern = "overrated_despite_poor_play"

    if pattern is None:
        # Расхождения нет — поправка затухает.
        _decay_team_rating_correction(team_id, content_type, SuspiciousActivityFlag)
        return 0

    # Поправка пропорциональна разрыву, но не больше MAX_CORRECTION.
    magnitude = min(1.0, abs(rating_gap) / (min_gap * 2)) if min_gap else 0.0
    raw_correction = STATS_DIVERGENCE_MAX_CORRECTION * magnitude
    signed_correction = raw_correction if pattern == "underrated_despite_dominance" else -raw_correction

    correction_obj, _ = TeamRatingCorrection.objects.get_or_create(team_id=team_id)
    correction_obj.correction = round(signed_correction, 3)
    correction_obj.last_pattern = pattern
    correction_obj.save(update_fields=["correction", "last_pattern", "updated_at"])

    _sync_divergence_flag(
        SuspiciousActivityFlag, content_type, team_id, "stats_divergence", round(magnitude, 2),
        {
            "pattern": pattern,
            "window_matches": len(window_pairs),
            "window_avg_rating": round(window_rating, 2),
            "baseline_avg_rating": round(baseline_mean, 2),
            "window_avg_dominance_share": round(window_dominance, 2),
            "correction_applied": round(signed_correction, 3),
        },
    )
    return 1


# ============================================================================
# Детектор расхождения для игроков: игрока сравниваем с его же нормой
# (оценка по статистике или наш индекс), а не с соперником.
# ============================================================================

PLAYER_STATS_DIVERGENCE_LOOKBACK_DAYS = 120
PLAYER_STATS_DIVERGENCE_WINDOW_MATCHES = 5
PLAYER_STATS_DIVERGENCE_MIN_WINDOW_MATCHES = 3
PLAYER_STATS_DIVERGENCE_BASELINE_MIN_MATCHES = 5
PLAYER_STATS_DIVERGENCE_MIN_BASELINE_OBJECTIVE_SAMPLES = 3
PLAYER_STATS_DIVERGENCE_RATING_Z_THRESHOLD = 0.75
PLAYER_STATS_DIVERGENCE_MIN_RATING_GAP = 0.5
# Порог в стандартных отклонениях от нормы игрока.
PLAYER_STATS_DIVERGENCE_OBJECTIVE_Z_THRESHOLD = 0.6

# Небольшая поправка, как у команды.
PLAYER_STATS_DIVERGENCE_MAX_CORRECTION = 0.4
PLAYER_STATS_DIVERGENCE_CORRECTION_DECAY = 0.5
PLAYER_STATS_DIVERGENCE_CORRECTION_FLOOR = 0.02
PLAYER_STATS_DIVERGENCE_DISMISS_COOLDOWN_DAYS = 30

# Веса запасного индекса. Одинаковы для всех амплуа — игрок сравнивается сам с собой.
PLAYER_OBJECTIVE_GOAL_WEIGHT = 3.0
PLAYER_OBJECTIVE_ASSIST_WEIGHT = 2.0
PLAYER_OBJECTIVE_OWN_GOAL_WEIGHT = -3.0
PLAYER_OBJECTIVE_RED_CARD_WEIGHT = -2.0
PLAYER_OBJECTIVE_YELLOW_CARD_WEIGHT = -0.5
PLAYER_OBJECTIVE_SHOT_ON_TARGET_WEIGHT = 0.5
PLAYER_OBJECTIVE_SAVE_WEIGHT = 0.4
PLAYER_OBJECTIVE_FOUL_WEIGHT = -0.2
PLAYER_OBJECTIVE_MISSED_PENALTY_WEIGHT = -1.0

# Метрики из raw. BLOCKED_SHOTS не используем — непонятно, чьи это удары.
PLAYER_OBJECTIVE_RAW_WEIGHTS = {
    "TACKLES": 0.3,
    "INTERCEPTIONS": 0.3,
    "CLEARANCES": 0.15,
    "DUELS_WON": 0.1,
    "DUELS_LOST": -0.05,
    "AERIALS_WON": 0.1,
    "KEY_PASSES": 0.4,
    "BIG_CHANCES_CREATED": 0.7,
    "SUCCESSFUL_DRIBBLES": 0.2,
    "DISPOSSESSED": -0.1,
    "ERROR_LEAD_TO_SHOT": -1.0,
    "SAVES_INSIDE_BOX": 0.2,
}

# Минимум матчей с оценкой по статистике в окне и в норме.
PLAYER_EXTERNAL_RATING_MIN_SAMPLES = 3


def _player_external_rating(match_id, player_id) -> float | None:
    """Оценка игрока по статистике за матч (raw["RATING"], шкала 1-10) или None."""
    raw = (
        MatchPlayerStatistics.objects.filter(match_id=match_id, player_id=player_id)
        .values_list("raw", flat=True).first()
    ) or {}
    value = raw.get("RATING")
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _calibrated_objective_weights() -> dict | None:
    """Откалиброванные веса из настроек (player_objective_weights) или None."""
    import json

    from core.models import get_setting

    raw = get_setting("player_objective_weights", "")
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return {"intercept": float(data["intercept"]), "weights": {k: float(v) for k, v in data["weights"].items()}}
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


def _player_objective_score(match_id, player_id) -> float | None:
    """Наш индекс игры за матч (события + статистика). Сравнивается только с
    историей того же игрока.

    :return: None, если по игроку в матче нет никаких данных.
    """
    stats = MatchPlayerStatistics.objects.filter(match_id=match_id, player_id=player_id).first()
    event_types = list(
        MatchEvent.objects.filter(match_id=match_id, player_id=player_id).values_list("event_type", flat=True)
    )
    assist_count = MatchEvent.objects.filter(match_id=match_id, assist_player_id=player_id).count()

    if stats is None and not event_types and not assist_count:
        return None

    # Есть откалиброванные веса и полный raw — считаем по ним (шкала 1-10).
    calibrated = _calibrated_objective_weights()
    if calibrated and stats is not None and (stats.raw or {}).get("MINUTES_PLAYED") is not None:
        raw = stats.raw or {}
        total = calibrated["intercept"]
        for key, weight in calibrated["weights"].items():
            value = raw.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                total += value * weight
        return total

    score = 0.0
    score += event_types.count("goal") * PLAYER_OBJECTIVE_GOAL_WEIGHT
    score += event_types.count("penalty") * PLAYER_OBJECTIVE_GOAL_WEIGHT
    score += assist_count * PLAYER_OBJECTIVE_ASSIST_WEIGHT
    score += event_types.count("own_goal") * PLAYER_OBJECTIVE_OWN_GOAL_WEIGHT
    score += event_types.count("red_card") * PLAYER_OBJECTIVE_RED_CARD_WEIGHT
    score += event_types.count("yellow_card") * PLAYER_OBJECTIVE_YELLOW_CARD_WEIGHT

    if stats is not None:
        score += (stats.shots_on_target or 0) * PLAYER_OBJECTIVE_SHOT_ON_TARGET_WEIGHT
        score += (stats.saves or 0) * PLAYER_OBJECTIVE_SAVE_WEIGHT
        score += (stats.fouls or 0) * PLAYER_OBJECTIVE_FOUL_WEIGHT
        # Защитные и созидательные метрики из raw.
        raw = stats.raw or {}
        for key, weight in PLAYER_OBJECTIVE_RAW_WEIGHTS.items():
            value = raw.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                score += value * weight
        score += (stats.missed_penalty or 0) * PLAYER_OBJECTIVE_MISSED_PENALTY_WEIGHT

    return score


@shared_task
def detect_player_rating_stats_divergence_task() -> int:
    """Детектор расхождения по всем игрокам, игравшим за последние LOOKBACK_DAYS."""
    from users.models import SuspiciousActivityFlag

    since = timezone.now() - timedelta(days=PLAYER_STATS_DIVERGENCE_LOOKBACK_DAYS)
    active_player_ids = list(
        PlayerMatchAggregate.objects.filter(match__status="finished", match__start_time__gte=since)
        .values_list("player_id", flat=True)
        .distinct()
    )
    if not active_player_ids:
        return 0

    content_type = ContentType.objects.get_for_model(Player)
    flagged = 0
    for player_id in active_player_ids:
        flagged += _check_player_stats_divergence(player_id, content_type, SuspiciousActivityFlag)

    if flagged:
        logger.warning("Player stats-divergence antifraud: flagged %d player signal(s).", flagged)
    return flagged


def _decay_player_rating_correction(player_id, content_type=None, SuspiciousActivityFlag=None) -> None:
    """Паттерн не подтвердился — поправка игрока затухает."""
    correction_obj = PlayerRatingCorrection.objects.filter(player_id=player_id).first()
    if correction_obj is None or correction_obj.correction == 0.0:
        _mark_divergence_flag_inactive(SuspiciousActivityFlag, content_type, player_id, "player_stats_divergence", 0.0)
        return
    new_value = correction_obj.correction * PLAYER_STATS_DIVERGENCE_CORRECTION_DECAY
    if abs(new_value) < PLAYER_STATS_DIVERGENCE_CORRECTION_FLOOR:
        new_value = 0.0
    correction_obj.correction = round(new_value, 3)
    correction_obj.last_pattern = ""
    correction_obj.save(update_fields=["correction", "last_pattern", "updated_at"])
    _mark_divergence_flag_inactive(SuspiciousActivityFlag, content_type, player_id, "player_stats_divergence", new_value)


def _check_player_stats_divergence(player_id, content_type, SuspiciousActivityFlag) -> int:
    """Один игрок: оценки последних матчей против его нормы и против объективной игры.

    Играл лучше нормы, а оценки ниже — возможна накрутка против; хуже, а оценки
    выше — за. При срабатывании обновляет PlayerRatingCorrection и флаг.
    """
    existing_correction = PlayerRatingCorrection.objects.filter(player_id=player_id).first()
    if existing_correction and existing_correction.suppressed_until and existing_correction.suppressed_until > timezone.now():
        return 0

    fetch_limit = max(PLAYER_STATS_DIVERGENCE_WINDOW_MATCHES, PLAYER_STATS_DIVERGENCE_BASELINE_MIN_MATCHES) * 3
    aggregates = list(
        PlayerMatchAggregate.objects.filter(
            player_id=player_id, match__status="finished", total_votes__gte=min_votes_for_display(),
        )
        .select_related("match")
        .order_by("-match__start_time")[:fetch_limit]
    )
    # Норма — только по матчам старше окна.
    if len(aggregates) < PLAYER_STATS_DIVERGENCE_WINDOW_MATCHES + PLAYER_STATS_DIVERGENCE_BASELINE_MIN_MATCHES:
        return 0

    baseline_pool = aggregates[PLAYER_STATS_DIVERGENCE_WINDOW_MATCHES:]
    baseline_scores = [_raw_community_score(a) for a in baseline_pool]
    baseline_mean = sum(baseline_scores) / len(baseline_scores)
    baseline_std = calculate_std_dev(baseline_scores)

    window_aggs = aggregates[:PLAYER_STATS_DIVERGENCE_WINDOW_MATCHES]

    # Основной сигнал — оценка по статистике, если её хватает и в окне, и в норме.
    # Смешивать её с нашим индексом нельзя — разные шкалы.
    ext_baseline = [r for r in (_player_external_rating(a.match_id, player_id) for a in baseline_pool) if r is not None]
    ext_window = [
        (_raw_community_score(a), r) for a, r in
        ((a, _player_external_rating(a.match_id, player_id)) for a in window_aggs) if r is not None
    ]
    if (
        len(ext_baseline) >= PLAYER_EXTERNAL_RATING_MIN_SAMPLES
        and len(ext_window) >= PLAYER_STATS_DIVERGENCE_MIN_WINDOW_MATCHES
    ):
        objective_source = "sportmonks_rating"
        baseline_objective = ext_baseline
        window_pairs: list[tuple[float, float]] = ext_window
    else:
        objective_source = "composite"
        # Норма нашего индекса.
        baseline_objective = [
            obj for obj in (
                _player_objective_score(agg.match_id, player_id) for agg in baseline_pool
            ) if obj is not None
        ]
        window_pairs = []
        for agg in window_aggs:
            obj = _player_objective_score(agg.match_id, player_id)
            if obj is None:
                continue
            window_pairs.append((_raw_community_score(agg), obj))

    if len(baseline_objective) < PLAYER_STATS_DIVERGENCE_MIN_BASELINE_OBJECTIVE_SAMPLES:
        return 0  # мало данных для нормы
    obj_baseline_mean = sum(baseline_objective) / len(baseline_objective)
    obj_baseline_std = calculate_std_dev(baseline_objective) or 1.0  # ровная история — без деления на 0

    if len(window_pairs) < PLAYER_STATS_DIVERGENCE_MIN_WINDOW_MATCHES:
        return 0  # мало матчей в окне

    window_rating = sum(p[0] for p in window_pairs) / len(window_pairs)
    window_objective = sum(p[1] for p in window_pairs) / len(window_pairs)
    rating_gap = window_rating - baseline_mean
    objective_z = (window_objective - obj_baseline_mean) / obj_baseline_std

    min_gap = max(PLAYER_STATS_DIVERGENCE_MIN_RATING_GAP, PLAYER_STATS_DIVERGENCE_RATING_Z_THRESHOLD * baseline_std)

    pattern = None
    if objective_z >= PLAYER_STATS_DIVERGENCE_OBJECTIVE_Z_THRESHOLD and rating_gap <= -min_gap:
        pattern = "underrated_despite_stats"
    elif objective_z <= -PLAYER_STATS_DIVERGENCE_OBJECTIVE_Z_THRESHOLD and rating_gap >= min_gap:
        pattern = "overrated_despite_stats"

    if pattern is None:
        _decay_player_rating_correction(player_id, content_type, SuspiciousActivityFlag)
        return 0

    magnitude = min(1.0, abs(rating_gap) / (min_gap * 2)) if min_gap else 0.0
    raw_correction = PLAYER_STATS_DIVERGENCE_MAX_CORRECTION * magnitude
    signed_correction = raw_correction if pattern == "underrated_despite_stats" else -raw_correction

    correction_obj, _ = PlayerRatingCorrection.objects.get_or_create(player_id=player_id)
    correction_obj.correction = round(signed_correction, 3)
    correction_obj.last_pattern = pattern
    correction_obj.save(update_fields=["correction", "last_pattern", "updated_at"])

    _sync_divergence_flag(
        SuspiciousActivityFlag, content_type, player_id, "player_stats_divergence", round(magnitude, 2),
        {
            "pattern": pattern,
            "objective_source": objective_source,
            "window_matches": len(window_pairs),
            "window_avg_rating": round(window_rating, 2),
            "baseline_avg_rating": round(baseline_mean, 2),
            "window_avg_objective": round(window_objective, 2),
            "baseline_avg_objective": round(obj_baseline_mean, 2),
            "objective_z": round(objective_z, 2),
            "correction_applied": round(signed_correction, 3),
        },
    )
    return 1


# ============================================================================
# Детектор расхождения для тренеров: оценки тренера против доли его команды
# в игре. Только флаг модератору, без авто-поправки.
# ============================================================================

COACH_STATS_DIVERGENCE_WINDOW_MATCHES = 6
COACH_STATS_DIVERGENCE_MIN_WINDOW_MATCHES = 4
COACH_STATS_DIVERGENCE_BASELINE_MIN_MATCHES = 6


def _coach_score(agg) -> float:
    """Оценка тренера за матч — среднее 4 шкал."""
    return (agg.avg_tactics + agg.avg_substitutions + agg.avg_management + agg.avg_impact) / 4


@shared_task
def detect_coach_rating_stats_divergence_task() -> int:
    from coaches.models import Coach
    from users.models import SuspiciousActivityFlag

    since = timezone.now() - timedelta(days=STATS_DIVERGENCE_LOOKBACK_DAYS)
    coach_ids = list(
        CoachMatchAggregate.objects.filter(match__status="finished", match__start_time__gte=since)
        .values_list("coach_id", flat=True).distinct()
    )
    content_type = ContentType.objects.get_for_model(Coach)
    flagged = 0
    for coach_id in coach_ids:
        flagged += _check_coach_stats_divergence(coach_id, content_type, SuspiciousActivityFlag)
    if flagged:
        logger.warning("Coach stats-divergence antifraud: flagged %d coach signal(s).", flagged)
    return flagged


def _check_coach_stats_divergence(coach_id, content_type, SuspiciousActivityFlag) -> int:
    from coaches.models import Coach

    coach = Coach.objects.filter(id=coach_id).only("id", "team_id").first()
    if coach is None or not coach.team_id:
        return 0

    # 30 дней после «Отклонить» тренера не проверяем.
    if SuspiciousActivityFlag.objects.filter(
        content_type=content_type, object_id=str(coach_id), source="coach_stats_divergence",
        status="dismissed", reviewed_at__gte=timezone.now() - timedelta(days=STATS_DIVERGENCE_DISMISS_COOLDOWN_DAYS),
    ).exists():
        return 0

    fetch_limit = (COACH_STATS_DIVERGENCE_WINDOW_MATCHES + COACH_STATS_DIVERGENCE_BASELINE_MIN_MATCHES) * 2
    aggregates = [
        a for a in CoachMatchAggregate.objects.filter(
            coach_id=coach_id, match__status="finished", total_votes__gte=min_votes_for_display(),
        )
        .select_related("match").order_by("-match__start_time")[:fetch_limit]
        # Берём только матчи текущей команды тренера.
        if coach.team_id in (a.match.home_team_id, a.match.away_team_id)
    ]
    if len(aggregates) < COACH_STATS_DIVERGENCE_WINDOW_MATCHES + COACH_STATS_DIVERGENCE_BASELINE_MIN_MATCHES:
        return 0

    baseline_pool = aggregates[COACH_STATS_DIVERGENCE_WINDOW_MATCHES:]
    baseline_scores = [_coach_score(a) for a in baseline_pool]
    baseline_mean = sum(baseline_scores) / len(baseline_scores)
    baseline_std = calculate_std_dev(baseline_scores)

    window_pairs: list[tuple[float, float]] = []
    for agg in aggregates[:COACH_STATS_DIVERGENCE_WINDOW_MATCHES]:
        own_stat = MatchTeamStatistics.objects.filter(match_id=agg.match_id, team_id=coach.team_id).first()
        opp_stat = MatchTeamStatistics.objects.filter(match_id=agg.match_id).exclude(team_id=coach.team_id).first()
        if own_stat is None or opp_stat is None:
            continue
        share = _team_dominance_share(own_stat, opp_stat)
        if share is not None:
            window_pairs.append((_coach_score(agg), share))

    if len(window_pairs) < COACH_STATS_DIVERGENCE_MIN_WINDOW_MATCHES:
        return 0

    window_rating = sum(p[0] for p in window_pairs) / len(window_pairs)
    window_dominance = sum(p[1] for p in window_pairs) / len(window_pairs)
    rating_gap = window_rating - baseline_mean
    min_gap = max(STATS_DIVERGENCE_MIN_RATING_GAP, STATS_DIVERGENCE_RATING_Z_THRESHOLD * baseline_std)

    pattern = None
    if window_dominance >= STATS_DIVERGENCE_DOMINANCE_HIGH and rating_gap <= -min_gap:
        pattern = "underrated_despite_dominance"
    elif window_dominance <= STATS_DIVERGENCE_DOMINANCE_LOW and rating_gap >= min_gap:
        pattern = "overrated_despite_poor_play"

    if pattern is None:
        _mark_divergence_flag_inactive(SuspiciousActivityFlag, content_type, coach_id, "coach_stats_divergence", 0.0)
        return 0

    magnitude = min(1.0, abs(rating_gap) / (min_gap * 2)) if min_gap else 0.0
    _sync_divergence_flag(
        SuspiciousActivityFlag, content_type, coach_id, "coach_stats_divergence", round(magnitude, 2),
        {
            "pattern": pattern,
            "window_matches": len(window_pairs),
            "window_avg_rating": round(window_rating, 2),
            "baseline_avg_rating": round(baseline_mean, 2),
            "window_avg_dominance_share": round(window_dominance, 2),
        },
    )
    return 1


# ============================================================================
# Всплеск крайних оценок судье: сравниваем с другими недавними матчами лиги
# (соседей по матчу у судьи нет).
# ============================================================================

REFEREE_SPIKE_BASELINE_MATCHES = 40
REFEREE_SPIKE_MIN_VOTES = 8


def _referee_extreme_ratio(values) -> float:
    return sum(1 for v in values if v is not None and (v <= 2 or v >= 9)) / len(values)


@shared_task
def detect_referee_vote_spikes_task() -> int:
    from referees.models import Referee
    from users.models import SuspiciousActivityFlag
    from users.tasks import ANTIFRAUD_CALIBRATED_THRESHOLDS, get_antifraud_threshold

    mad_threshold = get_antifraud_threshold(
        "vote_spike_mad_threshold", ANTIFRAUD_CALIBRATED_THRESHOLDS["vote_spike_mad_threshold"]["default"]
    )
    recent_match_ids = list(
        Match.objects.filter(status="finished", referee__isnull=False)
        .order_by("-start_time").values_list("id", flat=True)[:REFEREE_SPIKE_BASELINE_MATCHES]
    )
    votes_by_match: dict = defaultdict(list)
    for match_id, value in RefereeEvaluation.objects.filter(match_id__in=recent_match_ids).values_list(
        "match_id", "decision_quality"
    ):
        votes_by_match[match_id].append(value)
    eligible = {mid: vals for mid, vals in votes_by_match.items() if len(vals) >= REFEREE_SPIKE_MIN_VOTES}
    if len(eligible) < VOTE_SPIKE_MIN_SIBLINGS:
        return 0

    match_ids = list(eligible)
    ratios = [_referee_extreme_ratio(eligible[mid]) for mid in match_ids]
    z_scores = _modified_z_scores(ratios)
    referee_by_match = dict(Match.objects.filter(id__in=match_ids).values_list("id", "referee_id"))
    content_type = ContentType.objects.get_for_model(Referee)

    flagged = 0
    for match_id, ratio, z in zip(match_ids, ratios, z_scores):
        if z < mad_threshold:
            continue
        referee_id = referee_by_match.get(match_id)
        if not referee_id:
            continue
        exists = SuspiciousActivityFlag.objects.filter(
            content_type=content_type, object_id=str(referee_id), match_id=match_id, source="vote_spike",
        ).exists()
        if exists:
            continue
        SuspiciousActivityFlag.objects.create(
            user=None, content_type=content_type, object_id=str(referee_id), match_id=match_id,
            source="vote_spike", score=round(min(1.0, z / (mad_threshold * 2)), 2),
            details={
                "window_votes": len(eligible[match_id]),
                "extreme_ratio": round(ratio, 2),
                "compared_matches": len(match_ids),
            },
        )
        flagged += 1
    if flagged:
        logger.warning("Referee vote-spike antifraud: flagged %d signal(s).", flagged)
    return flagged


def strip_applied_corrections(model, entity_field: str, entity_ids) -> int:
    """Убирает уже вшитую авто-поправку из агрегатов сущностей (после «Отклонить»)."""
    updated = 0
    for agg in model.objects.filter(**{f"{entity_field}__in": entity_ids}).exclude(rating_correction_applied=0):
        applied = agg.rating_correction_applied or 0.0
        old_score = agg.performance_score
        agg.performance_score = round(old_score - applied, 2)
        fields = ["performance_score", "rating_correction_applied", "updated_at"]
        if model is PlayerMatchAggregate:
            agg.maturity_score = round(agg.maturity_score - applied, 2)
            # clutch пропорционален performance_score.
            if old_score:
                agg.clutch_index = round(agg.clutch_index * agg.performance_score / old_score, 2)
            fields += ["maturity_score", "clutch_index"]
        agg.rating_correction_applied = 0.0
        agg.save(update_fields=fields)
        updated += 1
    return updated


def apply_divergence_dismissal(flags) -> None:
    """Последствия «Отклонить» для сигналов расхождения — общая логика для админки и дашборда.

    Игрок/команда: поправка обнуляется (и в прошлых матчах), проверка на паузе.
    Тренер: поправки нет, пауза считается по дате отклонённого флага.
    """
    from aggregates.models import PlayerRatingCorrection, TeamRatingCorrection

    now = timezone.now()
    team_ct = ContentType.objects.get_for_model(Team)
    player_ct = ContentType.objects.get_for_model(Player)
    team_ids, player_ids = [], []
    for flag in flags:
        if flag.source == "stats_divergence" and flag.content_type_id == team_ct.id:
            team_ids.append(flag.object_id)
        elif flag.source == "player_stats_divergence" and flag.content_type_id == player_ct.id:
            player_ids.append(flag.object_id)
    if team_ids:
        TeamRatingCorrection.objects.filter(team_id__in=team_ids).update(
            correction=0.0, last_pattern="",
            suppressed_until=now + timedelta(days=STATS_DIVERGENCE_DISMISS_COOLDOWN_DAYS),
        )
        # Ложное срабатывание — поправку снимаем и с уже посчитанных матчей.
        strip_applied_corrections(TeamMatchAggregate, "team_id", team_ids)
    if player_ids:
        PlayerRatingCorrection.objects.filter(player_id__in=player_ids).update(
            correction=0.0, last_pattern="",
            suppressed_until=now + timedelta(days=PLAYER_STATS_DIVERGENCE_DISMISS_COOLDOWN_DAYS),
        )
        strip_applied_corrections(PlayerMatchAggregate, "player_id", player_ids)
