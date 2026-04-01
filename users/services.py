# users/services.py — ПОЛНОСТЬЮ ИСПРАВЛЕННЫЙ ФАЙЛ

from django.utils import timezone
from datetime import timedelta
from django.db.models import Count, Avg, Q, F
from users.models import UserBadge, UserXP
from evaluations.models import ContextEvaluation, PlayerEvaluation
import logging

logger = logging.getLogger(__name__)


def check_and_award_badges(user):
    """
    Проверяет и выдаёт достижения пользователю.
    Возвращает список объектов UserBadge, которые были СОЗДАНЫ (не существовали ранее).
    """
    awarded = []
    
    # === 1. Первая оценка ===
    if user.total_evaluations == 1:
        badge, created = UserBadge.objects.get_or_create(
            user=user,
            badge_type='first_evaluation'
        )
        if created:
            awarded.append(badge)
            logger.info(f"Badge awarded: first_evaluation to {user.username}")
    
    # === 2. Активный фанат: 10 матчей ===
    if user.total_evaluations >= 10:
        badge, created = UserBadge.objects.get_or_create(
            user=user,
            badge_type='active_fan_10'
        )
        if created:
            awarded.append(badge)
            logger.info(f"Badge awarded: active_fan_10 to {user.username}")
    
    # === 3. Хардкор фанат: 50 матчей ===
    if user.total_evaluations >= 50:
        badge, created = UserBadge.objects.get_or_create(
            user=user,
            badge_type='active_fan_50'
        )
        if created:
            awarded.append(badge)
            logger.info(f"Badge awarded: active_fan_50 to {user.username}")
    
    # === 4. Точный аналитик: средняя оценка близка к сообществу ===
    if user.total_evaluations >= 20:
        recent_matches = ContextEvaluation.objects.filter(
            user=user
        ).select_related('match').order_by('-created_at')[:20]
        
        accurate_count = 0
        for ctx in recent_matches:
            match = ctx.match
            community_avg = PlayerEvaluation.objects.filter(
                match=match
            ).aggregate(avg=Avg('contribution'))['avg']
            if not community_avg:
                continue
            user_avg = PlayerEvaluation.objects.filter(
                user=user, match=match
            ).aggregate(avg=Avg('contribution'))['avg']
            if user_avg and abs(user_avg - community_avg) <= 1.0:
                accurate_count += 1
        
        if accurate_count >= 16:  # 80% от 20
            badge, created = UserBadge.objects.get_or_create(
                user=user,
                badge_type='accurate_analyst'
            )
            if created:
                awarded.append(badge)
                logger.info(f"Badge awarded: accurate_analyst to {user.username}")
    
    # === 5. Без предвзятости: низкий bias_score ===
    if user.total_evaluations >= 15:
        biased_count = 0
        total_checked = 0
        contexts = ContextEvaluation.objects.filter(
            user=user,
            supported_team__isnull=False
        ).select_related('match').order_by('-created_at')[:15]
        
        for ctx in contexts:
            match = ctx.match
            supported = ctx.supported_team
            if not supported:
                continue
            total_checked += 1
            
            own_avg = PlayerEvaluation.objects.filter(
                user=user, match=match, player__team=supported
            ).aggregate(avg=Avg('contribution'))['avg'] or 0
            
            opponent = match.away_team if match.home_team == supported else match.home_team
            opp_avg = PlayerEvaluation.objects.filter(
                user=user, match=match, player__team=opponent
            ).aggregate(avg=Avg('contribution'))['avg'] or 0
            
            if abs(own_avg - opp_avg) > 4:
                biased_count += 1
        
        if total_checked >= 10 and biased_count <= 2:
            badge, created = UserBadge.objects.get_or_create(
                user=user,
                badge_type='bias_free'
            )
            if created:
                awarded.append(badge)
                logger.info(f"Badge awarded: bias_free to {user.username}")
    
    # === 6. Ранняя пташка: оценка в первые 2 часа после матча ===
    early_evals = ContextEvaluation.objects.filter(
        user=user,
        created_at__lte=F('match__end_time') + timedelta(hours=2)
    ).count()
    if early_evals >= 5:
        badge, created = UserBadge.objects.get_or_create(
            user=user,
            badge_type='early_bird'
        )
        if created:
            awarded.append(badge)
            logger.info(f"Badge awarded: early_bird to {user.username}")
    
    # === 7. Серия: 7 дней подряд ===
    if user.evaluation_streak >= 7:
        badge, created = UserBadge.objects.get_or_create(
            user=user,
            badge_type='streak_7'
        )
        if created:
            awarded.append(badge)
            logger.info(f"Badge awarded: streak_7 to {user.username}")
    
    # === 8. Серия: 30 дней подряд ===
    if user.evaluation_streak >= 30:
        badge, created = UserBadge.objects.get_or_create(
            user=user,
            badge_type='streak_30'
        )
        if created:
            awarded.append(badge)
            logger.info(f"Badge awarded: streak_30 to {user.username}")
    
    return awarded  # Возвращаем объекты UserBadge, а не строки!


def calculate_level_xp_threshold(level):
    """XP, необходимый для перехода на следующий уровень"""
    return 100 * level


def get_level_progress(user):
    """Возвращает прогресс до следующего уровня"""
    xp = getattr(user, 'xp', None)
    if not xp:
        return {'current': 0, 'next': 100, 'percent': 0, 'level': 1}
    
    next_threshold = calculate_level_xp_threshold(xp.level)
    current_in_level = xp.total_xp - (100 * (xp.level - 1))
    percent = min(100, int((current_in_level / next_threshold) * 100))
    
    return {
        'current': current_in_level,
        'next': next_threshold,
        'percent': percent,
        'level': xp.level
    }