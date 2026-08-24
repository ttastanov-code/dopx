# aggregates/tasks.py
"""
Celery-задачи модуля aggregates.

Формула агрегата (вес пользователя, винзоризация хвостов, сегментация
свои/чужие/нейтральные) живёт в aggregates/services.py — здесь только
batch-upsert обвязка вокруг неё: bulk_create(update_conflicts=True) поверх
unique_*_match_aggregate — один upsert-запрос на весь матч вместо
update_or_create в цикле по каждой сущности (критически важно: задача
триггерится на каждое сохранение оценки, см. signals.py).

2026-08-23, продуктовый запрос "докрутить защиту рейтингов от сговора
фан-базы": до этой правки здесь был ОТДЕЛЬНЫЙ наивный дубль формулы —
обычное среднее без веса пользователя, без винзоризации, без сегментации,
никак не связанный с aggregates/services.py (та версия существовала
только в тестах). recalculate_coach_aggregates вообще не применял НИКАКОГО
веса. Теперь единственный источник формулы — aggregates/services.py,
здесь она просто вызывается для каждого типа сущности (игрок/тренер/
команда/судья) с одним и тем же движком. Заодно заведены
recalculate_team_aggregates/recalculate_referee_aggregates — раньше
команды и судьи вообще не имели персистентного агрегата за матч, рейтинг
считался live-Avg() без всякой защиты на каждый рендер страницы (см.
докстринги TeamMatchAggregate/RefereeMatchAggregate в aggregates/models.py).

detect_vote_velocity_anomalies_task — второй, независимый слой защиты:
MAD-детект аномального всплеска экстремальных оценок одной сущности
относительно остальных сущностей ТОГО ЖЕ матча в коротком окне. Вес
пользователя и IP-кластер (users/tasks.py) ловят либо ИЗВЕСТНЫХ по истории
предвзятых людей, либо фермы аккаунтов с одного IP — ни то, ни другое не
поймает свежую волну РЕАЛЬНЫХ людей с разных IP, которых позвали в
соцсетях/телеграм-чате занизить оценку конкретному игроку/команде/тренеру
после конкретного матча. Это именно тот сценарий, который описал продукт:
"футбольный клуб — большая организация с фан-базой, которая может по
сговору обрушить рейтинги".
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
    RefereeMatchAggregate,
    TeamMatchAggregate,
    TeamRatingCorrection,
)
from aggregates.services import (
    apply_neutral_anchor,
    build_user_weight_map,
    calculate_std_dev,
    calculate_weighted_average,
    segment_evaluations_by_side,
    segment_evaluations_by_side_multi,
)
from evaluations.models import (
    CoachEvaluation,
    ContextEvaluation,
    MatchEvaluation,
    PlayerEvaluation,
    RefereeEvaluation,
    TeamEvaluation,
)
from matches.models import Match, MatchTeamStatistics
from seasons.models import Season
from teams.models import Team, TeamSeasonStats

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


@shared_task(bind=True, max_retries=3, rate_limit="10/m")
def recalculate_player_aggregates(self, match_id: str) -> bool:
    """
    Пересчитывает агрегаты всех игроков матча: 1 запрос на выборку оценок +
    1 batch-upsert, независимо от числа игроков. Формула (вес, винзоризация,
    сегментация) — aggregates/services.py, weight_map строится ОДИН раз на
    весь матч (не зависит от того, какого игрока сейчас усредняем).

    :param match_id: UUID строкой (Celery не сериализует UUID напрямую).
    :return: True при успехе, False — матч/оценки не найдены или match_id невалиден.
    """
    try:
        match_uuid = uuid.UUID(match_id)
    except (ValueError, AttributeError, TypeError):
        logger.error("Invalid match_id passed to recalculate_player_aggregates: %r", match_id)
        return False

    match = Match.objects.filter(id=match_uuid).only(
        "id", "status", "home_team_id", "away_team_id"
    ).first()
    if not match:
        logger.error("Match not found: %s", match_id)
        return False

    logger.info("Starting player aggregate recalculation for match %s", match_id)

    evaluations = list(
        PlayerEvaluation.objects.filter(match_id=match_uuid)
        .select_related("user", "player")
        .only("user_id", "player_id", "contribution", "risk", "potential", "player__team_id")
    )

    if not evaluations:
        logger.info("No player evaluations for match %s", match_id)
        return True

    weight_map = build_user_weight_map(evaluations, match)

    # Группируем оценки по игроку одним проходом по списку.
    player_eval_map: dict[uuid.UUID, list[PlayerEvaluation]] = {}
    for eval_obj in evaluations:
        player_eval_map.setdefault(eval_obj.player_id, []).append(eval_obj)

    drama_index = _get_match_drama_index(match_id, match_uuid)  # раз на весь матч, не на каждого игрока

    now = timezone.now()
    aggregates_to_upsert: list[PlayerMatchAggregate] = []

    for player_id, player_evals in player_eval_map.items():
        avg_contribution = calculate_weighted_average(player_evals, "contribution", weight_map)
        avg_risk = calculate_weighted_average(player_evals, "risk", weight_map)
        avg_potential = calculate_weighted_average(player_evals, "potential", weight_map)

        # std_dev/stability — на СЫРЫХ голосах (не винзоризованных): разброс
        # мнений сам по себе полезный сигнал, обрезать его тут же, где
        # считаем "насколько мнения разошлись", было бы противоречиво.
        contributions = [e.contribution for e in player_evals]
        std_dev = calculate_std_dev(contributions)
        stability_index = 1.0 / std_dev if std_dev > 0 else 10.0

        player_team_id = player_evals[0].player.team_id
        # Один проход/один запрос сегментирует СРАЗУ contribution И risk —
        # risk_index (номинация "Игрок на грани", репутационно самая
        # чувствительная негативная номинация на сайте) защищаем тем же
        # нейтральным якорем, что и performance_score, а не только его.
        segments = segment_evaluations_by_side_multi(
            player_evals, ("contribution", "risk"), player_team_id, match
        )
        own_fans_avg, rival_fans_avg, neutral_avg, own_n, rival_n, neutral_n = segments["contribution"]
        _, _, neutral_risk_avg, risk_own_n, risk_rival_n, risk_neutral_n = segments["risk"]

        # performance_score/risk_index, в отличие от avg_contribution/avg_risk,
        # дополнительно утянуты к нейтральному якорю (apply_neutral_anchor)
        # при высокой доле пристрастных голосов — см. aggregates/services.py.
        # maturity/clutch считаются от УЖЕ скорректированных значений,
        # avg_contribution/avg_risk остаются "сырыми" взвешенными средними
        # для прозрачности (видно, насколько якорь изменил итог).
        performance_score = apply_neutral_anchor(
            avg_contribution, neutral_avg, own_n, rival_n, neutral_n
        )
        risk_index_value = apply_neutral_anchor(
            avg_risk, neutral_risk_avg, risk_own_n, risk_rival_n, risk_neutral_n
        )
        clutch_index = performance_score * (drama_index / 10.0)

        aggregates_to_upsert.append(
            PlayerMatchAggregate(
                # id используется только при INSERT; при конфликте Postgres
                # сохраняет id существующей строки.
                id=uuid.uuid4(),
                player_id=player_id,
                match_id=match_uuid,
                avg_contribution=round(avg_contribution, 2),
                avg_risk=round(avg_risk, 2),
                avg_potential=round(avg_potential, 2),
                total_votes=len(player_evals),
                performance_score=round(performance_score, 2),
                risk_index=round(risk_index_value, 2),
                maturity_score=round(performance_score - risk_index_value, 2),
                stability_index=round(stability_index, 2),
                clutch_index=round(clutch_index, 2),
                own_fans_avg=round(own_fans_avg, 2) if own_fans_avg is not None else None,
                rival_fans_avg=round(rival_fans_avg, 2) if rival_fans_avg is not None else None,
                neutral_avg=round(neutral_avg, 2) if neutral_avg is not None else None,
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
        "Upserted %d player aggregates for match %s in a single batch query (weighted+winsorized)",
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
    Пересчёт агрегатов тренеров матча тем же batch-upsert подходом, что и
    recalculate_player_aggregates — включая вес пользователя, винзоризацию
    и сегментацию по лагерю фаната (own_fans_avg/rival_fans_avg/neutral_avg),
    ДО 2026-08-23 отсутствовавшие здесь полностью (ни весов, ни сегментации).
    """
    try:
        match_uuid = uuid.UUID(match_id)
    except (ValueError, AttributeError, TypeError):
        logger.error("Invalid match_id passed to recalculate_coach_aggregates: %r", match_id)
        return False

    match = Match.objects.filter(id=match_uuid).only(
        "id", "home_team_id", "away_team_id"
    ).first()
    if not match:
        return False

    evaluations = list(
        CoachEvaluation.objects.filter(match_id=match_uuid)
        .select_related("user", "coach")
        .only(
            "user_id", "coach_id", "tactics", "substitutions",
            "game_management", "impact", "coach__team_id",
        )
    )

    if not evaluations:
        return True

    weight_map = build_user_weight_map(evaluations, match)

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

        coach_team_id = coach_evals[0].coach.team_id
        # CoachMatchAggregate не хранит отдельного composite performance_score
        # поля (average_score — Python @property поверх avg_*, не колонка
        # БД) — а номинации ("Тактический гений", "Мастер замен", см.
        # core/nominations.py) читают ИМЕННО avg_tactics/avg_substitutions
        # напрямую, а не composite. Поэтому якорим каждое из 4 полей
        # ПО ОТДЕЛЬНОСТИ (тот же apply_neutral_anchor, что и у игрока/
        # команды/судьи) — один проход segment_evaluations_by_side_multi
        # считает сегментацию сразу для всех 5 полей (composite +4 компонента).
        segments = segment_evaluations_by_side_multi(
            coach_evals,
            ("average_score", "tactics", "substitutions", "game_management", "impact"),
            coach_team_id,
            match,
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


@shared_task(bind=True, max_retries=3)
def recalculate_team_aggregates(self, match_id: str) -> bool:
    """
    Пересчёт агрегатов КОМАНД матча (TeamEvaluation) — новая задача,
    2026-08-23. До этой задачи у команд вообще не было персистентного
    агрегата за матч: teams/views.py считал Avg() напрямую по ВСЕЙ истории
    TeamEvaluation команды синхронно на каждый рендер страницы, без веса
    пользователя, без винзоризации, без защиты от сговора. См. докстринг
    TeamMatchAggregate в aggregates/models.py.
    """
    try:
        match_uuid = uuid.UUID(match_id)
    except (ValueError, AttributeError, TypeError):
        logger.error("Invalid match_id passed to recalculate_team_aggregates: %r", match_id)
        return False

    match = Match.objects.filter(id=match_uuid).only(
        "id", "home_team_id", "away_team_id"
    ).first()
    if not match:
        return False

    evaluations = list(
        TeamEvaluation.objects.filter(match_id=match_uuid)
        .select_related("user", "team")
        .only("user_id", "team_id", "tactics", "effort", "organization", "mentality")
    )

    if not evaluations:
        return True

    weight_map = build_user_weight_map(evaluations, match)

    team_eval_map: dict[uuid.UUID, list[TeamEvaluation]] = {}
    for eval_obj in evaluations:
        team_eval_map.setdefault(eval_obj.team_id, []).append(eval_obj)

    now = timezone.now()
    aggregates_to_upsert: list[TeamMatchAggregate] = []

    for team_id, team_evals in team_eval_map.items():
        avg_tactics = calculate_weighted_average(team_evals, "tactics", weight_map)
        avg_effort = calculate_weighted_average(team_evals, "effort", weight_map)
        avg_organization = calculate_weighted_average(team_evals, "organization", weight_map)
        avg_mentality = calculate_weighted_average(team_evals, "mentality", weight_map)
        # pooled_performance_score — среднее уже взвешенных/винзоризованных
        # полей, тот же принцип, что и average_score-property модели
        # (среднее 4 компонентов), просто на защищённых значениях.
        pooled_performance_score = (avg_tactics + avg_effort + avg_organization + avg_mentality) / 4

        own_fans_avg, rival_fans_avg, neutral_avg, own_n, rival_n, neutral_n = (
            segment_evaluations_by_side(team_evals, "average_score", team_id, match)
        )
        # Итоговый performance_score дополнительно утянут к нейтральному
        # якорю при высокой доле пристрастных голосов (apply_neutral_anchor,
        # aggregates/services.py) — тот же механизм, что и у игроков.
        performance_score = apply_neutral_anchor(
            pooled_performance_score, neutral_avg, own_n, rival_n, neutral_n
        )

        # Автоматическая, самозатухающая поправка от независимого внешнего
        # сигнала (detect_rating_stats_divergence_task, см. докстринг
        # TeamRatingCorrection в aggregates/models.py) — применяется ТОЛЬКО
        # здесь, на пересчёте, к БУДУЩИМ матчам, никогда не переписывая уже
        # сохранённые прошлые. Ограничена диапазоном оценки [1, 10].
        correction = TeamRatingCorrection.objects.filter(team_id=team_id).values_list(
            "correction", flat=True
        ).first() or 0.0
        if correction:
            performance_score = max(1.0, min(10.0, performance_score + correction))

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


@shared_task(bind=True, max_retries=3)
def recalculate_referee_aggregates(self, match_id: str) -> bool:
    """
    Пересчёт агрегата судейства матча — новая задача, 2026-08-23. Формула
    перенесена из season_squad/services.py::_build_referee_pool (была
    задублирована там и в referees/views.py, обе копии без веса
    пользователя): 0.6*decision_quality + 0.3*fairness + 0.1*(10 - influence/10).

    RefereeEvaluation НЕ хранит referee_id напрямую (оценивается судейство
    КОНКРЕТНОГО матча, судья берётся из match.referee) — поэтому здесь
    ровно один агрегат на матч, а не словарь по нескольким сущностям, как
    у игроков/тренеров/команд.

    Сегментация — не "свои/чужие" (у судьи нет своей команды), а
    home_fans_avg/away_fans_avg/neutral_avg: переиспользуем
    segment_evaluations_by_side, подставляя home_team_id как
    "entity_team_id" — тогда "свои" естественно превращаются в "фанаты
    домашней команды".
    """
    try:
        match_uuid = uuid.UUID(match_id)
    except (ValueError, AttributeError, TypeError):
        logger.error("Invalid match_id passed to recalculate_referee_aggregates: %r", match_id)
        return False

    match = Match.objects.filter(id=match_uuid).only(
        "id", "referee_id", "home_team_id", "away_team_id"
    ).first()
    if not match or not match.referee_id:
        return True  # матч без назначенного судьи — не ошибка, просто нечего агрегировать

    referee_evals = list(
        RefereeEvaluation.objects.filter(match_id=match_uuid)
        .select_related("user")
        .only("user_id", "influence_score", "decision_quality")
    )
    if not referee_evals:
        return True

    weight_map = build_user_weight_map(referee_evals, match)

    avg_influence = calculate_weighted_average(referee_evals, "influence_score", weight_map)
    avg_decision_quality = calculate_weighted_average(referee_evals, "decision_quality", weight_map)

    # Fairness приходит из ДРУГОЙ модели (MatchEvaluation — общая оценка
    # матча, не привязана к конкретному судье напрямую) — строим
    # ОТДЕЛЬНЫЙ weight_map по её собственным оценкам, тот же принцип, что
    # и у recalculate_match_aggregate.
    match_evals = list(
        MatchEvaluation.objects.filter(match_id=match_uuid)
        .select_related("user")
        .only("user_id", "fairness")
    )
    if match_evals:
        fairness_weight_map = build_user_weight_map(match_evals, match)
        avg_fairness = calculate_weighted_average(match_evals, "fairness", fairness_weight_map)
    else:
        # Фолбэк на decision_quality — тот же приём, что был в
        # _build_referee_pool: не даём судье незаслуженный бонус/штраф
        # просто от отсутствия данных по несвязанному вопросу.
        avg_fairness = avg_decision_quality

    pooled_performance_score = (
        0.6 * avg_decision_quality + 0.3 * avg_fairness + 0.1 * (10 - avg_influence / 10)
    )

    home_fans_avg, away_fans_avg, neutral_avg, home_n, away_n, neutral_n = (
        segment_evaluations_by_side(referee_evals, "decision_quality", match.home_team_id, match)
    )
    # Тот же нейтральный якорь, что и у игроков/команд (apply_neutral_anchor,
    # aggregates/services.py) — у судьи "свои/чужие" естественно превращаются
    # в home_n/away_n (обе стороны матча могут быть предвзяты ПРОТИВ судьи
    # по-разному, якорь одинаково гасит обе тяги).
    performance_score = apply_neutral_anchor(
        pooled_performance_score, neutral_avg, home_n, away_n, neutral_n
    )

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


@shared_task(bind=True, max_retries=3)
def recalculate_match_aggregate(self, match_id: str) -> bool:
    """Пересчёт единственного (OneToOne) агрегата матча — веса пользователя
    (trust_score/полный просмотр) применяются и здесь: MatchEvaluation не
    привязана к конкретной команде, поэтому fan-bias часть веса не
    сработает (нужен match для calculate_user_weight, но не entity_team_id
    для сегментации — сегментации у общей оценки матча нет и не нужно)."""
    try:
        match_uuid = uuid.UUID(match_id)
    except (ValueError, AttributeError, TypeError):
        logger.error("Invalid match_id passed to recalculate_match_aggregate: %r", match_id)
        return False

    match = Match.objects.filter(id=match_uuid).only(
        "id", "home_team_id", "away_team_id"
    ).first()
    if not match:
        return False

    evaluations = list(
        MatchEvaluation.objects.filter(match_id=match_uuid)
        .select_related("user")
        .only("user_id", "entertainment", "tension", "fairness", "turning_point")
    )

    if not evaluations:
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

    weight_map = build_user_weight_map(evaluations, match)

    avg_entertainment = calculate_weighted_average(evaluations, "entertainment", weight_map)
    avg_tension = calculate_weighted_average(evaluations, "tension", weight_map)
    avg_fairness = calculate_weighted_average(evaluations, "fairness", weight_map)
    # turning_point — булево поле, взвешивание/винзоризация к нему
    # неприменимы (не числовая шкала 1-10) — оставляем простой долей, как
    # было.
    turning_point_ratio = sum(1 for e in evaluations if e.turning_point) / len(evaluations)
    drama_index = avg_entertainment * avg_tension

    MatchAggregate.objects.update_or_create(
        match_id=match_uuid,
        defaults={
            "avg_entertainment": round(avg_entertainment, 2),
            "avg_tension": round(avg_tension, 2),
            "avg_fairness": round(avg_fairness, 2),
            "turning_point_ratio": round(turning_point_ratio, 2),
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


@shared_task(bind=True, max_retries=3)
def recalculate_all_aggregates_for_match(self, match_id: str) -> bool:
    """Полный пересчёт всех агрегатов матча: Match(→Player) / Coach / Team / Referee."""
    logger.info("Starting full aggregate recalculation for match %s", match_id)
    recalculate_match_aggregate.delay(match_id)
    recalculate_coach_aggregates.delay(match_id)
    recalculate_team_aggregates.delay(match_id)
    recalculate_referee_aggregates.delay(match_id)
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
    """
    Пересчёт турнирной таблицы сезона. При season_id=None (вызов из
    CELERY_BEAT_SCHEDULE каждые 10 минут) пересчитываются ВСЕ активные
    сезоны, а не только первый попавшийся — на случай нескольких лиг
    одновременно (уникальность активного сезона на лигу гарантирует
    Season.save(), см. seasons/models.py).
    """
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
    """Пересчёт турнирной таблицы ОДНОГО сезона (без изменений в логике — уже оптимизировано)."""
    season_id = season.id
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


# ============================================================
# Anti-brigading Phase 2: детект координированных всплесков голосования
# ============================================================

# Как долго матч считается "активным" для детекта всплесков — то же окно,
# что и у recalculate_all_aggregates (24 часа после закрытия голосования):
# сговор обычно случается в первые часы после матча, пока эмоции свежие.
VOTE_SPIKE_LOOKBACK_HOURS = 24

# Окно, в котором ищем ВСПЛЕСК (не всю историю голосования матча, а именно
# короткий промежуток — сигнатура призыва "все идём сейчас минусовать") —
# короче, чем окно IP-кластера (24ч), потому что организованный призыв в
# соцсетях/телеграм-чате обычно даёт всплеск в течение 1-3 часов.
VOTE_SPIKE_WINDOW_HOURS = 2

# Минимум голосов В ОКНЕ у сущности, чтобы её extreme_ratio вообще
# учитывался — на 1-2 голосах "100% экстремальных" ничего не значит.
VOTE_SPIKE_MIN_WINDOW_VOTES = 4

# Минимум СЕСТРИНСКИХ сущностей того же матча (остальные игроки/команды/
# тренеры), чтобы можно было посчитать медиану/MAD для сравнения — на
# матче с 1-2 оценёнными игроками "аномалия относительно остальных"
# статистически бессмысленна.
VOTE_SPIKE_MIN_SIBLINGS = 5

# Порог MAD-ز-скора (модифицированный z-score на основе медианы и MAD,
# устойчивый к выбросам в отличие от обычного std) — 3.5 общепринятый
# порог для "статистически значимый выброс" (Iglewicz & Hoaglin). Это
# ЗНАЧЕНИЕ ПО УМОЛЧАНИЮ/стартовая точка калибровки — фактически
# используемое значение читается через users.tasks.get_antifraud_threshold
# и еженедельно подстраивается users.tasks.recalibrate_antifraud_thresholds
# на основе решений модератора (см. её докстринг и users.models.
# AntiFraudThreshold). Эта константа остаётся как default для первого
# запуска/фолбэка, а не как обязательное действующее число.
VOTE_SPIKE_MAD_THRESHOLD = 3.5

# Экстремальными на шкале 1-10 считаем края: 1-2 (минус) и 9-10 (плюс).
EXTREME_LOW_MAX = 2
EXTREME_HIGH_MIN = 9


def _extreme_ratio(values: list[float]) -> float:
    """Доля значений на экстремальных краях шкалы 1-10."""
    if not values:
        return 0.0
    extreme = sum(1 for v in values if v <= EXTREME_LOW_MAX or v >= EXTREME_HIGH_MIN)
    return extreme / len(values)


def _modified_z_scores(values: list[float]) -> list[float]:
    """
    Модифицированный z-score по медиане/MAD (Iglewicz & Hoaglin) —
    устойчив к выбросам В САМОЙ выборке (обычный std сам "разъезжается"
    от одного экстремального значения и маскирует его же). Возвращает
    список |z| той же длины, что values.
    """
    if len(values) < 2:
        return [0.0] * len(values)
    median = statistics.median(values)
    abs_deviations = [abs(v - median) for v in values]
    mad = statistics.median(abs_deviations)
    if mad == 0:
        # MAD=0 (много одинаковых значений) — фолбэк на среднее абсолютное
        # отклонение, иначе делим на ноль и теряем сигнал именно там, где
        # выборка особенно однородна (что само по себе не аномалия).
        mean_abs_dev = sum(abs_deviations) / len(abs_deviations)
        if mean_abs_dev == 0:
            return [0.0] * len(values)
        return [abs(d) / (1.253314 * mean_abs_dev) for d in abs_deviations]
    return [0.6745 * d / mad for d in abs_deviations]


@shared_task
def detect_vote_velocity_anomalies_task() -> int:
    """
    Anti-brigading, 2026-08-23: ищет сущности (игрок/команда/тренер) с
    аномальным всплеском доли экстремальных оценок (1-2 или 9-10) в
    коротком окне (VOTE_SPIKE_WINDOW_HOURS) — сигнатура координированного
    призыва проголосовать против/за конкретного человека или команду,
    а НЕ бот-фермы (ту ловит IP-кластер, users/tasks.py::detect_ip_clusters_task)
    и НЕ индивидуальной предвзятости (её ловит calculate_user_weight по
    истории пользователя).

    Ключевая идея — сравнение НЕ с фиксированным порогом, а с остальными
    сущностями ТОГО ЖЕ МАТЧА (MAD-based модифицированный z-score):
    нормализация на контекст конкретного матча "бесплатно" учитывает
    драматичность игры, дерби-накал и т.д. — то, что было бы сложно
    откалибровать фиксированным глобальным порогом. Реально плохая игра
    одного футболиста и так даст много низких оценок у ВСЕХ игроков этой
    команды — аномалией считается именно ВЫБИВАЮЩАЯСЯ ИЗ РЯДА сущность,
    а не "кто-то получил плохие оценки".

    Не блокирует и не пересчитывает рейтинг напрямую (это уже делает
    винзоризация в aggregates/services.py, независимо от этого сигнала) —
    только создаёт SuspiciousActivityFlag(source="vote_spike") с
    generic-FK на сущность, для ручного разбора модератором
    (dashboard/antifraud).
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

    # Самокалибрующийся порог (см. users.tasks.recalibrate_antifraud_thresholds)
    # — VOTE_SPIKE_MAD_THRESHOLD остаётся значением по умолчанию/стартовой
    # точкой калибровки, а не обязательным действующим числом. Читаем один
    # раз на весь прогон (не на каждый матч) — порог общий для всей платформы.
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
    """Один матч: считает extreme_ratio в окне по каждой сущности (игрок/
    команда/тренер отдельно — сравнивать доли экстремальных оценок игроков
    с оценками тренеров было бы некорректно, разная шкала ожиданий), ищет
    MAD-выбросы, создаёт/обновляет флаги."""
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
            continue  # недостаточно "соседей" в матче, чтобы отличить норму от выброса

        entity_ids = list(eligible.keys())
        ratios = [_extreme_ratio(eligible[eid]) for eid in entity_ids]
        z_scores = _modified_z_scores(ratios)

        # get_for_model() — кэшированный лукап ContentType (Django держит
        # process-level кэш), не отдельный SELECT на каждый (матч, тип сущности).
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


# ============================================================
# Anti-brigading Phase 3: независимый внешний сигнал — расхождение
# рейтинга сообщества с объективной статистикой матчей (KFF)
# ============================================================
#
# 2026-08-23, продуктовый запрос "может, использовать статистику на KFF
# как независимый сигнал?": все предыдущие детекторы (vote_spike,
# ip_cluster, extreme_bias, градуированный штраф веса) в конечном счёте
# смотрят на САМИ ГОЛОСА — умный координированный сговор, который ставит
# не крайние 1/10, а просто "чуть завышенные/заниженные" оценки (8-10
# своим, 5 чужим — сценарий, который прямо описал пользователь), может
# оставаться НИЖЕ порогов всех этих детекторов сразу. У KFF есть
# ОБЪЕКТИВНЫЕ факты матча (удары, угловые и т.д.), которые вообще не
# зависят от голосов DOPX — их нельзя обмануть, договорившись в чате
# ставить "умеренные" оценки. Если рейтинг команды у сообщества устойчиво
# идёт вразрез с тем, как команда объективно играла последние несколько
# матчей — это проверяемый, объяснимый признак предвзятости.
#
# Метрика "доля доминирования" (dominance share) намеренно НЕ использует
# сырые счётчики (у разных матчей разная интенсивность игры) — только
# ДОЛЮ команды в сумме показателей обеих команд ЭТОГО ЖЕ матча (own /
# (own + opponent)), 0.5 = паритет. Это делает метрику самокалиброванной
# без ручной настройки "нормальных" значений ударов/угловых.
#
# Как и все остальные анти-фрод сигналы — это МЯГКИЙ сигнал для очереди
# ручной модерации (SuspiciousActivityFlag), НЕ автоматическая коррекция
# рейтинга: xG/пас часто null у KFF, у команды может быть объективно
# оправданная причина (травмы, судейство, перестройка состава) — решение
# всегда за модератором (dashboard/antifraud), не за кодом.

STATS_DIVERGENCE_LOOKBACK_DAYS = 120
STATS_DIVERGENCE_WINDOW_MATCHES = 8
STATS_DIVERGENCE_MIN_WINDOW_MATCHES = 5
STATS_DIVERGENCE_BASELINE_MIN_MATCHES = 8
STATS_DIVERGENCE_DOMINANCE_HIGH = 0.58
STATS_DIVERGENCE_DOMINANCE_LOW = 0.42
STATS_DIVERGENCE_RATING_Z_THRESHOLD = 0.75
STATS_DIVERGENCE_MIN_RATING_GAP = 0.5

# 2026-08-24: TeamRatingCorrection — автоматическая поправка (см. её
# докстринг в aggregates/models.py), применяется в recalculate_team_
# aggregates ниже. STATS_DIVERGENCE_MAX_CORRECTION — тот же порядок
# величины, что и NEUTRAL_ANCHOR_MAX_PULL (aggregates/services.py) —
# сознательно НЕБОЛЬШАЯ поправка, она не может сама по себе развернуть
# рейтинг, только скорректировать его в разумных пределах.
# STATS_DIVERGENCE_CORRECTION_DECAY — во сколько раз поправка уменьшается
# на каждом прогоне (раз в сутки), если паттерн команды на этот день
# не подтвердился — самозатухание, а не ручное "выключение".
STATS_DIVERGENCE_MAX_CORRECTION = 0.4
STATS_DIVERGENCE_CORRECTION_DECAY = 0.5
STATS_DIVERGENCE_CORRECTION_FLOOR = 0.02  # ниже этого значения поправка обнуляется, а не тлеет вечно

# Компоненты "доли доминирования" — намеренно ограничены двумя полями,
# которые у KFF заполнены стабильнее всего на уровне команды (пас/xG
# часто null, см. докстринг MatchTeamStatistics). Удары в створ — прямой
# показатель созидания, угловые — давления/территориального контроля.
DOMINANCE_SHARE_FIELDS = ("shots_on_goal", "corners")


def _team_dominance_share(own_stat: MatchTeamStatistics, opponent_stat: MatchTeamStatistics) -> float | None:
    """Средняя доля команды в сумме показателей обеих команд по
    DOMINANCE_SHARE_FIELDS за один матч. None, если ни по одному полю
    нет данных сразу у ОБЕИХ команд (типично для матчей без детальной
    статистики от KFF)."""
    shares = []
    for field in DOMINANCE_SHARE_FIELDS:
        own = getattr(own_stat, field)
        opp = getattr(opponent_stat, field)
        if own is None or opp is None:
            continue
        total = own + opp
        shares.append(0.5 if total == 0 else own / total)
    if not shares:
        return None
    return sum(shares) / len(shares)


@shared_task
def detect_rating_stats_divergence_task() -> int:
    """
    Для каждой "активной" команды (сыграла завершённый матч за последние
    STATS_DIVERGENCE_LOOKBACK_DAYS дней) сравнивает тренд рейтинга
    сообщества за последние STATS_DIVERGENCE_WINDOW_MATCHES матчей с её же
    долгосрочной нормой (baseline) — и с тем, как команда объективно
    играла (dominance share) в этих же матчах. Устойчивое расхождение в
    ОБЕИХ направлениях считается сигналом:
    - объективно доминировала, а рейтинг сообщества ниже её нормы —
      возможна предвзятость/накрутка фанатов СОПЕРНИКА;
    - объективно играла слабо, а рейтинг выше её нормы — возможна
      предвзятость/накрутка СВОИХ фанатов.

    2026-08-24: обнаруженный паттерн теперь не только создаёт флаг в
    очереди модерации, но и САМ обновляет TeamRatingCorrection —
    небольшую, ограниченную и самозатухающую поправку, которую
    recalculate_team_aggregates ниже прибавляет к performance_score на
    каждом следующем пересчёте. Модератору НЕ нужно ничего делать вручную
    для каждого срабатывания — флаг остаётся только для прозрачности и
    возможности отменить поправку (действие "Отклонить"), если решил, что
    расхождение объяснимо.

    См. докстринг блока выше — независим от самих голосов, в отличие от
    vote_spike/ip_cluster/extreme_bias.
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


def _decay_team_rating_correction(team_id) -> None:
    """Паттерн на этот прогон не подтвердился (или данных не хватило) —
    существующая поправка (если есть) затухает в STATS_DIVERGENCE_
    CORRECTION_DECAY раз, а не остаётся висеть навсегда. Не создаёт новую
    запись, если её и так не было (незачем заводить строку с нулём)."""
    correction_obj = TeamRatingCorrection.objects.filter(team_id=team_id).first()
    if correction_obj is None or correction_obj.correction == 0.0:
        return
    new_value = correction_obj.correction * STATS_DIVERGENCE_CORRECTION_DECAY
    if abs(new_value) < STATS_DIVERGENCE_CORRECTION_FLOOR:
        new_value = 0.0
    correction_obj.correction = round(new_value, 3)
    correction_obj.last_pattern = ""
    correction_obj.save(update_fields=["correction", "last_pattern", "updated_at"])


def _check_team_stats_divergence(team_id, content_type, SuspiciousActivityFlag) -> int:
    """Одна команда: считает baseline (долгосрочная норма рейтинга) и
    тренд последних матчей (рейтинг + объективное доминирование).
    При устойчивом расхождении САМА обновляет TeamRatingCorrection (см. её
    докстринг в aggregates/models.py) и создаёт флаг для прозрачности; при
    отсутствии паттерна — затухает существующую поправку, если она была.
    См. докстринг задачи выше."""
    fetch_limit = max(STATS_DIVERGENCE_WINDOW_MATCHES, STATS_DIVERGENCE_BASELINE_MIN_MATCHES) * 3
    aggregates = list(
        TeamMatchAggregate.objects.filter(team_id=team_id, match__status="finished")
        .select_related("match")
        .order_by("-match__start_time")[:fetch_limit]
    )
    if len(aggregates) < STATS_DIVERGENCE_BASELINE_MIN_MATCHES:
        return 0  # недостаточно истории, чтобы вообще судить — поправку не трогаем

    baseline_scores = [a.performance_score for a in aggregates]
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
        window_pairs.append((agg.performance_score, share))

    if len(window_pairs) < STATS_DIVERGENCE_MIN_WINDOW_MATCHES:
        return 0  # недостаточно матчей с объективной статистикой с обеих сторон — поправку не трогаем

    window_rating = sum(p[0] for p in window_pairs) / len(window_pairs)
    window_dominance = sum(p[1] for p in window_pairs) / len(window_pairs)
    rating_gap = window_rating - baseline_mean

    # Порог — доля от СОБСТВЕННОЙ дисперсии рейтинга команды (не общий
    # фиксированный порог для всех команд), но не ниже абсолютного пола —
    # иначе у команды с исторически ровным рейтингом (baseline_std ~ 0)
    # любое минимальное колебание считалось бы аномалией.
    min_gap = max(STATS_DIVERGENCE_MIN_RATING_GAP, STATS_DIVERGENCE_RATING_Z_THRESHOLD * baseline_std)

    pattern = None
    if window_dominance >= STATS_DIVERGENCE_DOMINANCE_HIGH and rating_gap <= -min_gap:
        pattern = "underrated_despite_dominance"
    elif window_dominance <= STATS_DIVERGENCE_DOMINANCE_LOW and rating_gap >= min_gap:
        pattern = "overrated_despite_poor_play"

    if pattern is None:
        # Данных было достаточно, но сегодня расхождения нет — если раньше
        # была поправка, она сама угасает, а не остаётся зашитой навсегда.
        _decay_team_rating_correction(team_id)
        return 0

    # Величина поправки пропорциональна тому, насколько разрыв превышает
    # порог, но жёстко ограничена STATS_DIVERGENCE_MAX_CORRECTION — это
    # НЕБОЛЬШАЯ автоматическая компенсация, а не переписывание рейтинга.
    magnitude = min(1.0, abs(rating_gap) / (min_gap * 2)) if min_gap else 0.0
    raw_correction = STATS_DIVERGENCE_MAX_CORRECTION * magnitude
    signed_correction = raw_correction if pattern == "underrated_despite_dominance" else -raw_correction

    correction_obj, _ = TeamRatingCorrection.objects.get_or_create(team_id=team_id)
    correction_obj.correction = round(signed_correction, 3)
    correction_obj.last_pattern = pattern
    correction_obj.save(update_fields=["correction", "last_pattern", "updated_at"])

    already_pending = SuspiciousActivityFlag.objects.filter(
        content_type=content_type, object_id=str(team_id), source="stats_divergence", status="pending",
    ).exists()
    if already_pending:
        return 1  # поправка уже обновлена выше, лишний дублирующий флаг не создаём

    SuspiciousActivityFlag.objects.create(
        user=None,
        content_type=content_type,
        object_id=str(team_id),
        match=None,
        source="stats_divergence",
        score=round(magnitude, 2),
        details={
            "pattern": pattern,
            "window_matches": len(window_pairs),
            "window_avg_rating": round(window_rating, 2),
            "baseline_avg_rating": round(baseline_mean, 2),
            "window_avg_dominance_share": round(window_dominance, 2),
            "correction_applied": round(signed_correction, 3),
        },
    )
    return 1
