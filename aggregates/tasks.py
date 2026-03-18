# aggregates/tasks.py
from celery import shared_task
from django.db.models import Avg, Count, F
from django.utils import timezone
from django.core.cache import cache
import logging
import math
from datetime import timedelta

from evaluations.models import (
    PlayerEvaluation,
    CoachEvaluation,
    MatchEvaluation,
    ContextEvaluation
)
from matches.models import Match
from aggregates.models import (
    PlayerMatchAggregate,
    CoachMatchAggregate,
    MatchAggregate
)
from users.models import User

logger = logging.getLogger(__name__)


def calculate_user_weight(user: User, context_eval: ContextEvaluation) -> float:
    """
    Расчёт веса голоса пользователя
    
    Формула:
    - 1.0 базовый
    - +0.2 если watched_type == full
    - +0.2 если trust_score > 1.2
    - -0.3 если выявлена фанатская системность
    """
    weight = 1.0
    
    if context_eval and context_eval.watched_type == 'full':
        weight += 0.2
    
    if user.trust_score > 1.2:
        weight += 0.2
    
    # TODO: Детекция фанатской системности
    # if detect_fan_bias(user, match):
    #     weight -= 0.3
    
    return max(0.5, weight)  # Минимальный вес 0.5


def calculate_std_dev(values):
    """Расчёт стандартного отклонения"""
    if len(values) < 2:
        return 0.0
    
    mean = sum(values) / len(values)
    variance = sum((x - mean) ** 2 for x in values) / len(values)
    return math.sqrt(variance)


@shared_task(bind=True, max_retries=3)
def recalculate_player_aggregates(self, match_id: str):
    """
    Пересчёт агрегатов для всех игроков конкретного матча
    
    Args:
        match_id: UUID матча в строковом формате
    """
    try:
        from uuid import UUID
        match_uuid = UUID(match_id)
    except (ValueError, AttributeError):
        logger.error(f"Invalid match_id: {match_id}")
        return False
    
    match = Match.objects.filter(id=match_uuid).first()
    if not match:
        logger.error(f"Match not found: {match_id}")
        return False
    
    logger.info(f"Starting player aggregate recalculation for match {match_id}")
    
    # Получаем все оценки игроков для этого матча
    evaluations = PlayerEvaluation.objects.filter(
        match_id=match_uuid
    ).select_related('user', 'player', 'match')
    
    if not evaluations.exists():
        logger.info(f"No player evaluations for match {match_id}")
        return True
    
    # Группируем по игрокам
    player_ids = evaluations.values_list('player_id', flat=True).distinct()
    
    updated_count = 0
    for player_id in player_ids:
        player_evals = evaluations.filter(player_id=player_id)
        
        if not player_evals.exists():
            continue
        
        # Получаем context evaluations для всех пользователей
        user_ids = player_evals.values_list('user_id', flat=True)
        context_evals = ContextEvaluation.objects.filter(
            user_id__in=user_ids,
            match_id=match_uuid
        )
        context_evals_map = {ce.user_id: ce for ce in context_evals}
        
        # Расчёт взвешенных средних
        total_weight = 0.0
        weighted_contribution = 0.0
        weighted_risk = 0.0
        weighted_potential = 0.0
        contributions = []
        
        for eval_obj in player_evals:
            user = eval_obj.user
            context_eval = context_evals_map.get(user.id)
            weight = calculate_user_weight(user, context_eval)
            
            weighted_contribution += eval_obj.contribution * weight
            weighted_risk += eval_obj.risk * weight
            weighted_potential += eval_obj.potential * weight
            total_weight += weight
            contributions.append(eval_obj.contribution)
        
        if total_weight == 0:
            continue
        
        avg_contribution = weighted_contribution / total_weight
        avg_risk = weighted_risk / total_weight
        avg_potential = weighted_potential / total_weight
        
        # Расчёт стандартного отклонения для stability index
        std_dev = calculate_std_dev(contributions)
        stability_index = 1.0 / std_dev if std_dev > 0 else 10.0
        
        # Получаем drama index из матча
        match_agg = MatchAggregate.objects.filter(match_id=match_uuid).first()
        drama_index = match_agg.drama_index if match_agg else 5.0
        
        # Вычисляемые индексы
        performance_score = avg_contribution
        risk_index = avg_risk
        maturity_score = avg_contribution - avg_risk
        clutch_index = avg_contribution * (drama_index / 10.0)  # Нормализация
        
        # Обновляем или создаём агрегат
        aggregate, created = PlayerMatchAggregate.objects.update_or_create(
            player_id=player_id,
            match_id=match_uuid,
            defaults={
                'avg_contribution': round(avg_contribution, 2),
                'avg_risk': round(avg_risk, 2),
                'avg_potential': round(avg_potential, 2),
                'total_votes': player_evals.count(),
                'performance_score': round(performance_score, 2),
                'risk_index': round(risk_index, 2),
                'maturity_score': round(maturity_score, 2),
                'stability_index': round(stability_index, 2),
                'clutch_index': round(clutch_index, 2),
            }
        )
        
        updated_count += 1
        
        # Инвалидация кэша для этого игрока
        cache.delete(f'player_aggregate_{player_id}_{match_id}')
    
    logger.info(f"Updated {updated_count} player aggregates for match {match_id}")
    return True


@shared_task(bind=True, max_retries=3)
def recalculate_coach_aggregates(self, match_id: str):
    """Пересчёт агрегатов для тренеров матча"""
    try:
        from uuid import UUID
        match_uuid = UUID(match_id)
    except (ValueError, AttributeError):
        logger.error(f"Invalid match_id: {match_id}")
        return False
    
    match = Match.objects.filter(id=match_uuid).first()
    if not match:
        return False
    
    evaluations = CoachEvaluation.objects.filter(
        match_id=match_uuid
    ).select_related('user', 'coach')
    
    if not evaluations.exists():
        return True
    
    coach_ids = evaluations.values_list('coach_id', flat=True).distinct()
    
    for coach_id in coach_ids:
        coach_evals = evaluations.filter(coach_id=coach_id)
        
        user_ids = coach_evals.values_list('user_id', flat=True)
        context_evals = ContextEvaluation.objects.filter(
            user_id__in=user_ids,
            match_id=match_uuid
        )
        context_evals_map = {ce.user_id: ce for ce in context_evals}
        
        total_weight = 0.0
        weighted_tactics = 0.0
        weighted_substitutions = 0.0
        weighted_management = 0.0
        weighted_impact = 0.0
        
        for eval_obj in coach_evals:
            user = eval_obj.user
            context_eval = context_evals_map.get(user.id)
            weight = calculate_user_weight(user, context_eval)
            
            weighted_tactics += eval_obj.tactics * weight
            weighted_substitutions += eval_obj.substitutions * weight
            weighted_management += eval_obj.game_management * weight
            weighted_impact += eval_obj.impact * weight
            total_weight += weight
        
        if total_weight == 0:
            continue
        
        aggregate, _ = CoachMatchAggregate.objects.update_or_create(
            coach_id=coach_id,
            match_id=match_uuid,
            defaults={
                'avg_tactics': round(weighted_tactics / total_weight, 2),
                'avg_substitutions': round(weighted_substitutions / total_weight, 2),
                'avg_management': round(weighted_management / total_weight, 2),
                'avg_impact': round(weighted_impact / total_weight, 2),
                'total_votes': coach_evals.count(),
            }
        )
        
        cache.delete(f'coach_aggregate_{coach_id}_{match_id}')
    
    return True


@shared_task(bind=True, max_retries=3)
def recalculate_match_aggregate(self, match_id: str):
    """Пересчёт агрегатов для матча"""
    try:
        from uuid import UUID
        match_uuid = UUID(match_id)
    except (ValueError, AttributeError):
        logger.error(f"Invalid match_id: {match_id}")
        return False
    
    match = Match.objects.filter(id=match_uuid).first()
    if not match:
        return False
    
    evaluations = MatchEvaluation.objects.filter(
        match_id=match_uuid
    ).select_related('user')
    
    if not evaluations.exists():
        # Создаём пустой агрегат
        MatchAggregate.objects.update_or_create(
            match_id=match_uuid,
            defaults={
                'avg_entertainment': 0.0,
                'avg_tension': 0.0,
                'avg_fairness': 0.0,
                'turning_point_ratio': 0.0,
                'total_votes': 0,
                'drama_index': 0.0,
            }
        )
        return True
    
    user_ids = evaluations.values_list('user_id', flat=True)
    context_evals = ContextEvaluation.objects.filter(
        user_id__in=user_ids,
        match_id=match_uuid
    )
    context_evals_map = {ce.user_id: ce for ce in context_evals}
    
    total_weight = 0.0
    weighted_entertainment = 0.0
    weighted_tension = 0.0
    weighted_fairness = 0.0
    turning_point_count = 0
    
    for eval_obj in evaluations:
        user = eval_obj.user
        context_eval = context_evals_map.get(user.id)
        weight = calculate_user_weight(user, context_eval)
        
        weighted_entertainment += eval_obj.entertainment * weight
        weighted_tension += eval_obj.tension * weight
        weighted_fairness += eval_obj.fairness * weight
        total_weight += weight
        
        if eval_obj.turning_point:
            turning_point_count += 1
    
    if total_weight == 0:
        return True
    
    avg_entertainment = weighted_entertainment / total_weight
    avg_tension = weighted_tension / total_weight
    avg_fairness = weighted_fairness / total_weight
    turning_point_ratio = turning_point_count / evaluations.count()
    drama_index = avg_entertainment * avg_tension
    
    aggregate, _ = MatchAggregate.objects.update_or_create(
        match_id=match_uuid,
        defaults={
            'avg_entertainment': round(avg_entertainment, 2),
            'avg_tension': round(avg_tension, 2),
            'avg_fairness': round(avg_fairness, 2),
            'turning_point_ratio': round(turning_point_ratio, 2),
            'total_votes': evaluations.count(),
            'drama_index': round(drama_index, 2),
        }
    )
    
    cache.delete(f'match_aggregate_{match_id}')
    
    # После пересчёта матча нужно пересчитать игроков (нужен drama_index)
    recalculate_player_aggregates.delay(match_id)
    
    return True


@shared_task(bind=True, max_retries=3)
def recalculate_all_aggregates_for_match(self, match_id: str):
    """
    Полный пересчёт всех агрегатов для матча
    Порядок: Match -> Coach -> Player
    """
    logger.info(f"Starting full aggregate recalculation for match {match_id}")
    
    # 1. Сначала матч (нужен для drama_index)
    recalculate_match_aggregate.delay(match_id)
    
    # 2. Тренеры
    recalculate_coach_aggregates.delay(match_id)
    
    # 3. Игроки (будут запущены после match aggregate из recalculate_match_aggregate)
    
    logger.info(f"Queued aggregate recalculation tasks for match {match_id}")
    return True


@shared_task
def recalculate_all_aggregates():
    """
    Периодическая задача: пересчёт агрегатов для всех активных матчей
    Запускается каждые 10 минут через Celery Beat
    """
    logger.info("Starting periodic aggregate recalculation")
    
    # Находим матчи, где голосование ещё открыто или недавно закрылось
    now = timezone.now()
    active_matches = Match.objects.filter(
        voting_open_until__gte=now - timedelta(hours=24)
    ).values_list('id', flat=True)
    
    if not active_matches:
        logger.info("No active matches for aggregate recalculation")
        return 0
    
    count = 0
    for match_id in active_matches:
        recalculate_all_aggregates_for_match.delay(str(match_id))
        count += 1
    
    logger.info(f"Queued {count} match aggregate recalculation tasks")
    return count


@shared_task
def cleanup_old_sessions():
    """Очистка старых данных (кэш, сессии)"""
    logger.info("Running cleanup task")
    # Можно добавить очистку старых логов, кэша и т.д.
    return True


@shared_task(bind=True, max_retries=3)
def trigger_aggregate_recalculation(self, match_id: str):
    """
    Триггер для пересчёта агрегатов при изменении voting_open_until
    Используется в сигналах Django
    """
    try:
        recalculate_all_aggregates_for_match.delay(match_id)
        logger.info(f"Triggered aggregate recalculation for match {match_id}")
        return True
    except Exception as exc:
        logger.error(f"Error triggering recalculation: {exc}")
        raise self.retry(exc=exc, countdown=60)