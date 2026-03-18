# aggregates/services.py
from django.db.models import Avg, StdDev, Count, F
from django.utils import timezone
from evaluations.models import PlayerEvaluation, CoachEvaluation, MatchEvaluation
from .models import PlayerMatchAggregate, CoachMatchAggregate, MatchAggregate
from users.models import User
from evaluations.models import ContextEvaluation
import math


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


def detect_fan_bias(user: User, match) -> bool:
    """
    Детекция систематической предвзятости (фанатизма)
    TODO: Реализовать на основе истории оценок
    """
    # Пример: если пользователь всегда ставит макс. оценки одной команде
    return False


def calculate_weighted_average(evaluations, field_name, context_evals_map):
    """
    Расчёт взвешенного среднего значения
    
    Args:
        evaluations: QuerySet оценок
        field_name: имя поля для усреднения
        context_evals_map: dict {user_id: ContextEvaluation}
    """
    total_weight = 0.0
    weighted_sum = 0.0
    
    for eval_obj in evaluations:
        user = eval_obj.user
        context_eval = context_evals_map.get(user.id)
        weight = calculate_user_weight(user, context_eval)
        
        value = getattr(eval_obj, field_name, 0)
        weighted_sum += value * weight
        total_weight += weight
    
    if total_weight == 0:
        return 0.0
    
    return weighted_sum / total_weight


def calculate_std_dev(values):
    """Расчёт стандартного отклонения"""
    if len(values) < 2:
        return 0.0
    
    mean = sum(values) / len(values)
    variance = sum((x - mean) ** 2 for x in values) / len(values)
    return math.sqrt(variance)


def recalculate_player_aggregate(player, match):
    """Пересчёт агрегатов для игрока за матч"""
    evaluations = PlayerEvaluation.objects.filter(
        player=player,
        match=match
    ).select_related('user', 'match')
    
    if not evaluations.exists():
        return None
    
    # Получаем context evaluations для всех пользователей
    user_ids = evaluations.values_list('user_id', flat=True)
    context_evals = ContextEvaluation.objects.filter(
        user_id__in=user_ids,
        match=match
    )
    context_evals_map = {ce.user_id: ce for ce in context_evals}
    
    # Расчёт взвешенных средних
    avg_contribution = calculate_weighted_average(
        evaluations, 'contribution', context_evals_map
    )
    avg_risk = calculate_weighted_average(
        evaluations, 'risk', context_evals_map
    )
    avg_potential = calculate_weighted_average(
        evaluations, 'potential', context_evals_map
    )
    
    # Расчёт стандартного отклонения для stability index
    contributions = [e.contribution for e in evaluations]
    std_dev = calculate_std_dev(contributions)
    stability_index = 1.0 / std_dev if std_dev > 0 else 10.0
    
    # Получаем drama index из матча
    match_agg = MatchAggregate.objects.filter(match=match).first()
    drama_index = match_agg.drama_index if match_agg else 5.0
    
    # Вычисляемые индексы
    performance_score = avg_contribution
    risk_index = avg_risk
    maturity_score = avg_contribution - avg_risk
    clutch_index = avg_contribution * (drama_index / 10.0)  # Нормализация
    
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
    
    return aggregate


def recalculate_coach_aggregate(coach, match):
    """Пересчёт агрегатов для тренера за матч"""
    evaluations = CoachEvaluation.objects.filter(
        coach=coach,
        match=match
    ).select_related('user')
    
    if not evaluations.exists():
        return None
    
    user_ids = evaluations.values_list('user_id', flat=True)
    context_evals = ContextEvaluation.objects.filter(
        user_id__in=user_ids,
        match=match
    )
    context_evals_map = {ce.user_id: ce for ce in context_evals}
    
    avg_tactics = calculate_weighted_average(evaluations, 'tactics', context_evals_map)
    avg_substitutions = calculate_weighted_average(evaluations, 'substitutions', context_evals_map)
    avg_management = calculate_weighted_average(evaluations, 'game_management', context_evals_map)
    avg_impact = calculate_weighted_average(evaluations, 'impact', context_evals_map)
    
    aggregate, _ = CoachMatchAggregate.objects.update_or_create(
        coach=coach,
        match=match,
        defaults={
            'avg_tactics': round(avg_tactics, 2),
            'avg_substitutions': round(avg_substitutions, 2),
            'avg_management': round(avg_management, 2),
            'avg_impact': round(avg_impact, 2),
            'total_votes': evaluations.count(),
        }
    )
    
    return aggregate


def recalculate_match_aggregate(match):
    """Пересчёт агрегатов для матча"""
    evaluations = MatchEvaluation.objects.filter(
        match=match
    ).select_related('user')
    
    if not evaluations.exists():
        return None
    
    user_ids = evaluations.values_list('user_id', flat=True)
    context_evals = ContextEvaluation.objects.filter(
        user_id__in=user_ids,
        match=match
    )
    context_evals_map = {ce.user_id: ce for ce in context_evals}
    
    avg_entertainment = calculate_weighted_average(evaluations, 'entertainment', context_evals_map)
    avg_tension = calculate_weighted_average(evaluations, 'tension', context_evals_map)
    avg_fairness = calculate_weighted_average(evaluations, 'fairness', context_evals_map)
    
    turning_point_count = evaluations.filter(turning_point=True).count()
    turning_point_ratio = turning_point_count / evaluations.count() if evaluations.count() > 0 else 0.0
    
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
    
    return aggregate


def recalculate_all_aggregates_for_match(match):
    """Пересчёт всех агрегатов для матча"""
    # 1. Сначала матч (нужен для drama_index)
    match_agg = recalculate_match_aggregate(match)
    
    # 2. Игроки
    player_ids = PlayerEvaluation.objects.filter(
        match=match
    ).values_list('player_id', flat=True).distinct()
    
    for player_id in player_ids:
        from players.models import Player
        player = Player.objects.get(id=player_id)
        recalculate_player_aggregate(player, match)
    
    # 3. Тренеры
    if match.home_coach:
        recalculate_coach_aggregate(match.home_coach, match)
    if match.away_coach:
        recalculate_coach_aggregate(match.away_coach, match)
    
    return True