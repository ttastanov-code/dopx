# aggregates/services.py
from django.db.models import Avg, StdDev, Count, F, Sum, FloatField
from django.db.models.functions import Cast
from django.utils import timezone
from django.core.cache import cache
from evaluations.models import PlayerEvaluation, CoachEvaluation, MatchEvaluation, ContextEvaluation
from .models import PlayerMatchAggregate, CoachMatchAggregate, MatchAggregate
from users.models import User
import math
import logging

logger = logging.getLogger(__name__)


def calculate_user_weight_cached(user_id: int, context_eval_id: int) -> float:
    """
    OPTIMIZATION: Кэширование веса пользователя
    """
    cache_key = f'user_weight_{user_id}_{context_eval_id}'
    cached_weight = cache.get(cache_key)
    
    if cached_weight is not None:
        return cached_weight
    
    try:
        user = User.objects.only('trust_score').get(id=user_id)
        context_eval = ContextEvaluation.objects.only('watched_type').get(id=context_eval_id)
        
        weight = 1.0
        if context_eval and context_eval.watched_type == 'full':
            weight += 0.2
        if user.trust_score > 1.2:
            weight += 0.2
        
        weight = max(0.5, weight)
        
        # Кэширование на 1 час
        cache.set(cache_key, weight, timeout=3600)
        
        return weight
    except (User.DoesNotExist, ContextEvaluation.DoesNotExist):
        return 1.0


def calculate_weighted_average_optimized(evaluations_queryset, field_name: str, match_id: str) -> float:
    """
    OPTIMIZATION: 
    - Вычисление взвешенного среднего на уровне БД где возможно
    - Минимизация Python-обработок
    - Batch загрузка context evaluations
    """
    evaluations = list(evaluations_queryset.select_related('user').only(
        'user_id', field_name
    ))
    
    if not evaluations:
        return 0.0
    
    # Batch загрузка всех context evaluations одним запросом
    user_ids = [e.user_id for e in evaluations]
    context_evals = ContextEvaluation.objects.filter(
        user_id__in=user_ids,
        match_id=match_id
    ).only('user_id', 'watched_type')
    
    context_map = {ce.user_id: ce for ce in context_evals}
    
    # Получаем trust_score всех пользователей одним запросом
    users = User.objects.filter(
        id__in=user_ids
    ).only('id', 'trust_score')
    user_trust_map = {u.id: u.trust_score for u in users}
    
    total_weight = 0.0
    weighted_sum = 0.0
    
    for eval_obj in evaluations:
        user_id = eval_obj.user_id
        context_eval = context_map.get(user_id)
        
        # Расчёт веса без дополнительных запросов
        weight = 1.0
        if context_eval and context_eval.watched_type == 'full':
            weight += 0.2
        
        trust_score = user_trust_map.get(user_id, 1.0)
        if trust_score > 1.2:
            weight += 0.2
        
        weight = max(0.5, weight)
        
        value = getattr(eval_obj, field_name, 0) or 0
        weighted_sum += value * weight
        total_weight += weight
    
    if total_weight == 0:
        return 0.0
    
    return weighted_sum / total_weight


def calculate_std_dev_optimized(values: list) -> float:
    """
    OPTIMIZATION: Быстрый расчёт стандартного отклонения
    """
    n = len(values)
    if n < 2:
        return 0.0
    
    # Используем math.fsum для лучшей точности
    mean = math.fsum(values) / n
    variance = math.fsum((x - mean) ** 2 for x in values) / n
    
    return math.sqrt(variance) if variance > 0 else 0.0


def recalculate_player_aggregate(player, match):
    """
    OPTIMIZATION: 
    - Минимизация запросов к БД
    - Кэширование промежуточных результатов
    - Batch operations
    """
    match_id = str(match.id)
    cache_key = f'player_agg_calc_{player.id}_{match_id}'
    
    # Проверка кэша расчёта
    cached_result = cache.get(cache_key)
    if cached_result:
        return cached_result
    
    evaluations = PlayerEvaluation.objects.filter(
        player=player,
        match=match
    ).select_related('user')
    
    if not evaluations.exists():
        return None
    
    # Оптимизированный расчёт взвешенных средних
    avg_contribution = calculate_weighted_average_optimized(
        evaluations, 'contribution', match_id
    )
    avg_risk = calculate_weighted_average_optimized(
        evaluations, 'risk', match_id
    )
    avg_potential = calculate_weighted_average_optimized(
        evaluations, 'potential', match_id
    )
    
    # Расчёт стандартного отклонения
    contributions = [e.contribution for e in evaluations if e.contribution]
    std_dev = calculate_std_dev_optimized(contributions)
    stability_index = 1.0 / std_dev if std_dev > 0 else 10.0
    
    # Получаем drama index из матча (с кэшем)
    match_agg_cache = cache.get(f'match_aggregate_{match_id}')
    if match_agg_cache:
        drama_index = match_agg_cache.get('drama_index', 5.0)
    else:
        match_agg = MatchAggregate.objects.filter(match=match).only('drama_index').first()
        drama_index = match_agg.drama_index if match_agg else 5.0
    
    # Вычисляемые индексы
    performance_score = avg_contribution
    risk_index = avg_risk
    maturity_score = avg_contribution - avg_risk
    clutch_index = avg_contribution * (drama_index / 10.0)
    
    aggregate, _ = PlayerMatchAggregate.objects.update_or_create(
        player=player,
        match=match,
        defaults={
            'avg_contribution': round(avg_contribution, 2),
            'avg_risk': round(avg_risk, 2),
            'avg_potential': round(avg_potential, 2),
            'total_votes': evaluations.count(),
            'performance_score': round(performance_score, 2),
            'risk_index': round(risk_index, 2),
            'maturity_score': round(maturity_score, 2),
            'stability_index': round(stability_index, 2),
            'clutch_index': round(clutch_index, 2),
        }
    )
    
    # Кэширование результата расчёта
    result = {
        'id': str(aggregate.id),
        'performance_score': aggregate.performance_score,
        'total_votes': aggregate.total_votes
    }
    cache.set(cache_key, result, timeout=300)
    
    return aggregate


def recalculate_coach_aggregate(coach, match):
    """
    OPTIMIZATION: Оптимизированный расчёт агрегатов тренера
    """
    match_id = str(match.id)
    
    evaluations = CoachEvaluation.objects.filter(
        coach=coach,
        match=match
    ).select_related('user')
    
    if not evaluations.exists():
        return None
    
    user_ids = evaluations.values_list('user_id', flat=True)
    context_evals = ContextEvaluation.objects.filter(
        user_id__in=user_ids,
        match_id=match_id
    ).only('user_id', 'watched_type')
    context_map = {ce.user_id: ce for ce in context_evals}
    
    users = User.objects.filter(id__in=user_ids).only('id', 'trust_score')
    user_trust_map = {u.id: u.trust_score for u in users}
    
    total_weight = 0.0
    weighted_tactics = 0.0
    weighted_substitutions = 0.0
    weighted_management = 0.0
    weighted_impact = 0.0
    
    for eval_obj in evaluations:
        user_id = eval_obj.user_id
        context_eval = context_map.get(user_id)
        
        weight = 1.0
        if context_eval and context_eval.watched_type == 'full':
            weight += 0.2
        
        trust_score = user_trust_map.get(user_id, 1.0)
        if trust_score > 1.2:
            weight += 0.2
        
        weight = max(0.5, weight)
        
        weighted_tactics += eval_obj.tactics * weight
        weighted_substitutions += eval_obj.substitutions * weight
        weighted_management += eval_obj.game_management * weight
        weighted_impact += eval_obj.impact * weight
        total_weight += weight
    
    if total_weight == 0:
        return None
    
    aggregate, _ = CoachMatchAggregate.objects.update_or_create(
        coach=coach,
        match=match,
        defaults={
            'avg_tactics': round(weighted_tactics / total_weight, 2),
            'avg_substitutions': round(weighted_substitutions / total_weight, 2),
            'avg_management': round(weighted_management / total_weight, 2),
            'avg_impact': round(weighted_impact / total_weight, 2),
            'total_votes': evaluations.count(),
        }
    )
    
    return aggregate


def recalculate_match_aggregate(match):
    """
    OPTIMIZATION: Оптимизированный расчёт агрегатов матча
    """
    match_id = str(match.id)
    
    evaluations = MatchEvaluation.objects.filter(
        match=match
    ).select_related('user')
    
    if not evaluations.exists():
        # Создаём пустой агрегат
        aggregate, _ = MatchAggregate.objects.update_or_create(
            match=match,
            defaults={
                'avg_entertainment': 0.0,
                'avg_tension': 0.0,
                'avg_fairness': 0.0,
                'turning_point_ratio': 0.0,
                'total_votes': 0,
                'drama_index': 0.0,
            }
        )
        return aggregate
    
    user_ids = evaluations.values_list('user_id', flat=True)
    context_evals = ContextEvaluation.objects.filter(
        user_id__in=user_ids,
        match_id=match_id
    ).only('user_id', 'watched_type')
    context_map = {ce.user_id: ce for ce in context_evals}
    
    users = User.objects.filter(id__in=user_ids).only('id', 'trust_score')
    user_trust_map = {u.id: u.trust_score for u in users}
    
    total_weight = 0.0
    weighted_entertainment = 0.0
    weighted_tension = 0.0
    weighted_fairness = 0.0
    turning_point_count = 0
    
    for eval_obj in evaluations:
        user_id = eval_obj.user_id
        context_eval = context_map.get(user_id)
        
        weight = 1.0
        if context_eval and context_eval.watched_type == 'full':
            weight += 0.2
        
        trust_score = user_trust_map.get(user_id, 1.0)
        if trust_score > 1.2:
            weight += 0.2
        
        weight = max(0.5, weight)
        
        weighted_entertainment += eval_obj.entertainment * weight
        weighted_tension += eval_obj.tension * weight
        weighted_fairness += eval_obj.fairness * weight
        total_weight += weight
        
        if eval_obj.turning_point:
            turning_point_count += 1
    
    if total_weight == 0:
        return None
    
    avg_entertainment = weighted_entertainment / total_weight
    avg_tension = weighted_tension / total_weight
    avg_fairness = weighted_fairness / total_weight
    turning_point_ratio = turning_point_count / evaluations.count()
    drama_index = avg_entertainment * avg_tension
    
    aggregate, _ = MatchAggregate.objects.update_or_create(
        match=match,
        defaults={
            'avg_entertainment': round(avg_entertainment, 2),
            'avg_tension': round(avg_tension, 2),
            'avg_fairness': round(avg_fairness, 2),
            'turning_point_ratio': round(turning_point_ratio, 2),
            'total_votes': evaluations.count(),
            'drama_index': round(drama_index, 2),
        }
    )
    
    # Кэширование результата
    cache.set(f'match_aggregate_{match_id}', {
        'drama_index': drama_index,
        'avg_entertainment': avg_entertainment,
        'avg_tension': avg_tension,
    }, timeout=600)
    
    return aggregate


def recalculate_all_aggregates_for_match(match):
    """
    OPTIMIZATION: 
    - Правильный порядок расчёта (Match -> Player)
    - Минимизация запросов
    """
    # 1. Сначала матч (нужен для drama_index)
    recalculate_match_aggregate(match)
    
    # 2. Игроки — batch загрузка всех оценок
    player_ids = PlayerEvaluation.objects.filter(
        match=match
    ).values_list('player_id', flat=True).distinct()
    
    for player_id in player_ids:
        from players.models import Player
        player = Player.objects.only('id').get(id=player_id)
        recalculate_player_aggregate(player, match)
    
    # 3. Тренеры
    if match.home_coach:
        recalculate_coach_aggregate(match.home_coach, match)
    if match.away_coach:
        recalculate_coach_aggregate(match.away_coach, match)
    
    return True