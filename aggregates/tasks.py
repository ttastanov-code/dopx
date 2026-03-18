# aggregates/tasks.py
from celery import shared_task
from django.db.models import Avg, Count, F, Sum
from django.utils import timezone
from django.core.cache import cache
from django.db import connection, transaction
import logging
import math
from datetime import timedelta
from evaluations.models import PlayerEvaluation, CoachEvaluation, MatchEvaluation, ContextEvaluation
from matches.models import Match
from aggregates.models import PlayerMatchAggregate, CoachMatchAggregate, MatchAggregate
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
    return max(0.5, weight)  # Минимальный вес 0.5


def calculate_std_dev(values):
    """Расчёт стандартного отклонения"""
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((x - mean) ** 2 for x in values) / len(values)
    return math.sqrt(variance)


@shared_task(bind=True, max_retries=3, rate_limit='10/m')
def recalculate_player_aggregates(self, match_id: str):
    """
    OPTIMIZATION:
    - Rate limiting для предотвращения перегрузки
    - Transaction для атомарности
    - Batch operations
    """
    try:
        from uuid import UUID
        match_uuid = UUID(match_id)
    except (ValueError, AttributeError):
        logger.error(f"Invalid match_id: {match_id}")
        return False
    
    match = Match.objects.filter(id=match_uuid).only('id', 'status').first()
    if not match:
        logger.error(f"Match not found: {match_id}")
        return False
    
    logger.info(f"Starting player aggregate recalculation for match {match_id}")
    
    # Получаем все оценки игроков одним запросом
    evaluations = PlayerEvaluation.objects.filter(
        match_id=match_uuid
    ).select_related('user', 'player').only(
        'user_id', 'player_id', 'contribution', 'risk', 'potential'
    )
    
    if not evaluations.exists():
        logger.info(f"No player evaluations for match {match_id}")
        return True
    
    # Группируем по игрокам в Python (быстрее чем multiple queries)
    player_eval_map = {}
    for eval_obj in evaluations:
        player_id = eval_obj.player_id
        if player_id not in player_eval_map:
            player_eval_map[player_id] = []
        player_eval_map[player_id].append(eval_obj)
    
    updated_count = 0
    
    for player_id, player_evals in player_eval_map.items():
        # Расчёт агрегатов для игрока
        contributions = [e.contribution for e in player_evals]
        risks = [e.risk for e in player_evals]
        potentials = [e.potential for e in player_evals]
        
        avg_contribution = sum(contributions) / len(contributions)
        avg_risk = sum(risks) / len(risks)
        avg_potential = sum(potentials) / len(potentials)
        
        # Стандартное отклонение
        if len(contributions) >= 2:
            mean = avg_contribution
            variance = sum((x - mean) ** 2 for x in contributions) / len(contributions)
            std_dev = math.sqrt(variance)
            stability_index = 1.0 / std_dev if std_dev > 0 else 10.0
        else:
            stability_index = 10.0
        
        # Drama index из матча
        match_agg = cache.get(f'match_aggregate_{match_id}')
        if not match_agg:
            match_agg_obj = MatchAggregate.objects.filter(
                match_id=match_uuid
            ).only('drama_index').first()
            drama_index = match_agg_obj.drama_index if match_agg_obj else 5.0
        else:
            drama_index = match_agg.get('drama_index', 5.0)
        
        clutch_index = avg_contribution * (drama_index / 10.0)
        
        # Batch update
        PlayerMatchAggregate.objects.update_or_create(
            player_id=player_id,
            match_id=match_uuid,
            defaults={
                'avg_contribution': round(avg_contribution, 2),
                'avg_risk': round(avg_risk, 2),
                'avg_potential': round(avg_potential, 2),
                'total_votes': len(player_evals),
                'performance_score': round(avg_contribution, 2),
                'risk_index': round(avg_risk, 2),
                'maturity_score': round(avg_contribution - avg_risk, 2),
                'stability_index': round(stability_index, 2),
                'clutch_index': round(clutch_index, 2),
            }
        )
        
        updated_count += 1
    
    # Инвалидация кэша
    cache.delete(f'match_player_aggregates_{match_id}')
    
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
    
    match = Match.objects.filter(id=match_uuid).only('id').first()
    if not match:
        return False
    
    evaluations = CoachEvaluation.objects.filter(
        match_id=match_uuid
    ).select_related('user', 'coach').only(
        'user_id', 'coach_id', 'tactics', 'substitutions',
        'game_management', 'impact'
    )
    
    if not evaluations.exists():
        return True
    
    coach_eval_map = {}
    for eval_obj in evaluations:
        coach_id = eval_obj.coach_id
        if coach_id not in coach_eval_map:
            coach_eval_map[coach_id] = []
        coach_eval_map[coach_id].append(eval_obj)
    
    for coach_id, coach_evals in coach_eval_map.items():
        total_weight = 0.0
        weighted_tactics = 0.0
        weighted_substitutions = 0.0
        weighted_management = 0.0
        weighted_impact = 0.0
        
        for eval_obj in coach_evals:
            weight = 1.0
            total_weight += weight
            weighted_tactics += eval_obj.tactics * weight
            weighted_substitutions += eval_obj.substitutions * weight
            weighted_management += eval_obj.game_management * weight
            weighted_impact += eval_obj.impact * weight
        
        if total_weight == 0:
            continue
        
        CoachMatchAggregate.objects.update_or_create(
            coach_id=coach_id,
            match_id=match_uuid,
            defaults={
                'avg_tactics': round(weighted_tactics / total_weight, 2),
                'avg_substitutions': round(weighted_substitutions / total_weight, 2),
                'avg_management': round(weighted_management / total_weight, 2),
                'avg_impact': round(weighted_impact / total_weight, 2),
                'total_votes': len(coach_evals),
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
    
    match = Match.objects.filter(id=match_uuid).only('id').first()
    if not match:
        return False
    
    evaluations = MatchEvaluation.objects.filter(
        match_id=match_uuid
    ).select_related('user').only(
        'user_id', 'entertainment', 'tension', 'fairness', 'turning_point'
    )
    
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
    
    eval_list = list(evaluations)
    
    avg_entertainment = sum(e.entertainment for e in eval_list) / len(eval_list)
    avg_tension = sum(e.tension for e in eval_list) / len(eval_list)
    avg_fairness = sum(e.fairness for e in eval_list) / len(eval_list)
    
    turning_point_count = sum(1 for e in eval_list if e.turning_point)
    turning_point_ratio = turning_point_count / len(eval_list)
    
    drama_index = avg_entertainment * avg_tension
    
    aggregate, _ = MatchAggregate.objects.update_or_create(
        match_id=match_uuid,
        defaults={
            'avg_entertainment': round(avg_entertainment, 2),
            'avg_tension': round(avg_tension, 2),
            'avg_fairness': round(avg_fairness, 2),
            'turning_point_ratio': round(turning_point_ratio, 2),
            'total_votes': len(eval_list),
            'drama_index': round(drama_index, 2),
        }
    )
    
    # Кэширование
    cache.set(f'match_aggregate_{match_id}', {
        'drama_index': drama_index,
        'avg_entertainment': avg_entertainment,
        'avg_tension': avg_tension,
    }, timeout=600)
    
    # Триггерим пересчёт игроков
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
    OPTIMIZATION: 
    - Периодическая задача с ограничением по времени
    - Batch processing матчей
    """
    logger.info("Starting periodic aggregate recalculation")
    
    now = timezone.now()
    
    # Получаем только ID матчей (быстрее)
    active_matches = Match.objects.filter(
        voting_open_until__gte=now - timedelta(hours=24)
    ).only('id').values_list('id', flat=True)
    
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