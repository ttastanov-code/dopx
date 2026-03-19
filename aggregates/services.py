# aggregates/services.py
from django.db.models import Avg, Count, Q
from django.core.cache import cache
from django.utils import timezone
from evaluations.models import PlayerEvaluation, ContextEvaluation
from users.models import User
import math
import logging

logger = logging.getLogger(__name__)


def calculate_user_weight(user: User, context_eval: ContextEvaluation) -> float:
    """Расчёт веса голоса пользователя"""
    weight = 1.0
    
    # Бонус за полный просмотр
    if context_eval and context_eval.watched_type == 'full':
        weight += 0.2
    
    # Бонус за высокий trust_score
    if user.trust_score > 1.2:
        weight += 0.2
    
    # Штраф за фанатское смещение
    if context_eval and context_eval.match:
        if _detect_fan_bias(user, context_eval.match):
            weight -= 0.3
    
    return max(0.5, min(2.0, weight))


def _detect_fan_bias(user: User, match) -> bool:
    """
    Детекция фанатского смещения:
    Если пользователь систематически ставит макс. оценку своей команде
    и мин. — сопернику, это считается предвзятостью.
    """
    if not match:
        return False
    
    supported_team = getattr(
        ContextEvaluation.objects.filter(user=user, match=match).first(),
        'supported_team', None
    )
    if not supported_team:
        return False
    
    # Получить оценки пользователя за последние 10 матчей этой команды
    recent_matches = match.__class__.objects.filter(
        Q(home_team=supported_team) | Q(away_team=supported_team),
        status='finished'
    ).order_by('-start_time')[:10]
    
    if recent_matches.count() < 3:
        return False
    
    bias_score = 0
    for m in recent_matches:
        is_home = m.home_team == supported_team
        team_evals = PlayerEvaluation.objects.filter(
            user=user, match=m,
            player__team=supported_team
        ).values_list('contribution', flat=True)
        
        opponent_evals = PlayerEvaluation.objects.filter(
            user=user, match=m
        ).exclude(
            player__team=supported_team
        ).values_list('contribution', flat=True)
        
        if team_evals and opponent_evals:
            team_avg = sum(team_evals) / len(team_evals)
            opp_avg = sum(opponent_evals) / len(opponent_evals)
            if team_avg >= 9 and opp_avg <= 3:
                bias_score += 1
    
    # Если в 70%+ случаев наблюдается экстремальное смещение
    return bias_score >= len(recent_matches) * 0.7


def calculate_weighted_average(evaluations_queryset, field_name: str, match_id: str) -> float:
    """Взвешенное среднее с учётом веса пользователя"""
    evaluations = list(evaluations_queryset.select_related('user', 'user__context_evaluations'))
    if not evaluations:
        return 0.0
    
    total_weight = 0.0
    weighted_sum = 0.0
    
    for eval_obj in evaluations:
        context = eval_obj.user.context_evaluations.filter(match_id=match_id).first()
        weight = calculate_user_weight(eval_obj.user, context)
        value = getattr(eval_obj, field_name, 0) or 0
        weighted_sum += value * weight
        total_weight += weight
    
    return weighted_sum / total_weight if total_weight > 0 else 0.0


def calculate_std_dev(values: list) -> float:
    """Стандартное отклонение"""
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    variance = sum((x - mean) ** 2 for x in values) / n
    return math.sqrt(variance)


def recalculate_player_aggregate(player, match):
    """Пересчёт агрегатов игрока с защитой от накруток"""
    match_id = str(match.id)
    cache_key = f'player_agg_{player.id}_{match_id}'
    
    # Проверка кэша
    cached = cache.get(cache_key)
    if cached:
        return cached
    
    evaluations = PlayerEvaluation.objects.filter(
        player=player, match=match
    ).select_related('user')
    
    if not evaluations.exists():
        return None
    
    # Взвешенные средние
    avg_contribution = calculate_weighted_average(evaluations, 'contribution', match_id)
    avg_risk = calculate_weighted_average(evaluations, 'risk', match_id)
    avg_potential = calculate_weighted_average(evaluations, 'potential', match_id)
    
    # Стандартное отклонение (стабильность)
    contributions = [e.contribution for e in evaluations if e.contribution]
    std_dev = calculate_std_dev(contributions)
    stability_index = 1.0 / std_dev if std_dev > 0 else 10.0
    
    # Drama index из матча
    from .models import MatchAggregate
    drama_index = cache.get(f'match_agg_{match_id}')
    if not drama_index:
        match_agg = MatchAggregate.objects.filter(match=match).only('drama_index').first()
        drama_index = match_agg.drama_index if match_agg else 5.0
        cache.set(f'match_agg_{match_id}', drama_index, 600)
    
    # Вычисляемые индексы
    performance_score = avg_contribution
    maturity_score = avg_contribution - avg_risk
    clutch_index = avg_contribution * (drama_index / 10.0)
    
    # Сохранение
    from .models import PlayerMatchAggregate
    aggregate, _ = PlayerMatchAggregate.objects.update_or_create(
        player=player, match=match,
        defaults={
            'avg_contribution': round(avg_contribution, 2),
            'avg_risk': round(avg_risk, 2),
            'avg_potential': round(avg_potential, 2),
            'total_votes': evaluations.count(),
            'performance_score': round(performance_score, 2),
            'risk_index': round(avg_risk, 2),
            'maturity_score': round(maturity_score, 2),
            'stability_index': round(stability_index, 2),
            'clutch_index': round(clutch_index, 2),
        }
    )
    
    # Кэширование результата
    result = {
        'id': str(aggregate.id),
        'performance_score': aggregate.performance_score,
        'total_votes': aggregate.total_votes
    }
    cache.set(cache_key, result, 300)
    
    return aggregate

def calculate_user_trust_adjustment(user, match):
    """
    Расчёт корректировки trust_score пользователя
    Возвращает: float от -0.1 до +0.1
    """
    from evaluations.models import PlayerEvaluation
    from django.db.models import Avg
    
    # Получаем оценки пользователя за матч
    user_evals = PlayerEvaluation.objects.filter(
        user=user, match=match
    ).values_list('contribution', flat=True)
    
    if not user_evals:
        return 0.0
    
    # Получаем средние оценки сообщества за матч
    community_avg = PlayerEvaluation.objects.filter(
        match=match
    ).aggregate(avg=Avg('contribution'))['avg']
    
    if not community_avg:
        return 0.0
    
    # Считаем отклонение пользователя
    user_avg = sum(user_evals) / len(user_evals)
    deviation = abs(user_avg - community_avg)
    
    # Нормализуем отклонение (0-10 шкала)
    normalized_deviation = min(deviation / 5.0, 1.0)
    
    # Если близко к сообществу → +, если далеко → -
    if normalized_deviation < 0.3:
        return 0.05  # Адекватный аналитик
    elif normalized_deviation < 0.6:
        return 0.0  # Нормально
    else:
        return -0.05  # Предвзятый
    
def detect_fan_bias(user, match, supported_team=None):
    """
    Детекция фанатской предвзятости
    Возвращает: dict с метриками
    """
    from evaluations.models import PlayerEvaluation
    from django.db.models import Avg
    
    if not supported_team:
        from evaluations.models import ContextEvaluation
        context = ContextEvaluation.objects.filter(
            user=user, match=match
        ).first()
        supported_team = context.supported_team if context else None
    
    if not supported_team:
        return {'is_biased': False, 'score': 0.0}
    
    # Оценки игроков своей команды
    own_team_evals = PlayerEvaluation.objects.filter(
        user=user, match=match, player__team=supported_team
    ).aggregate(avg=Avg('contribution'))['avg'] or 0
    
    # Оценки игроков соперника
    opponent_team = match.away_team if match.home_team == supported_team else match.home_team
    opponent_evals = PlayerEvaluation.objects.filter(
        user=user, match=match, player__team=opponent_team
    ).aggregate(avg=Avg('contribution'))['avg'] or 0
    
    # Разница
    bias_score = own_team_evals - opponent_evals
    
    # Если разница > 4 — явная предвзятость
    is_biased = bias_score > 4.0
    
    return {
        'is_biased': is_biased,
        'score': bias_score,
        'own_team_avg': own_team_evals,
        'opponent_avg': opponent_evals,
    }