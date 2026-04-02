# users/services.py
from django.utils import timezone
from datetime import timedelta
from django.db.models import Count, Avg, F, Q
from users.models import UserBadge
from evaluations.models import ContextEvaluation, PlayerEvaluation
import logging

logger = logging.getLogger(__name__)


def check_and_award_badges(user):
    """
    Проверяет условия и выдаёт достижения.
    Возвращает список только что созданных объектов UserBadge.
    """
    awarded = []
    total = user.total_evaluations
    streak = user.evaluation_streak

    try:
        # 1. Первая оценка
        if total >= 1:
            b, created = UserBadge.objects.get_or_create(user=user, badge_type='first_evaluation')
            if created: awarded.append(b)

        # 2. Активный фанат (10)
        if total >= 10:
            b, created = UserBadge.objects.get_or_create(user=user, badge_type='active_fan_10')
            if created: awarded.append(b)

        # 3. Хардкор фанат (50)
        if total >= 50:
            b, created = UserBadge.objects.get_or_create(user=user, badge_type='active_fan_50')
            if created: awarded.append(b)

        # 4. Точный аналитик (отклонение < 1.0 в 80% из последних 20 матчей)
        if total >= 20:
            recent_matches = ContextEvaluation.objects.filter(user=user).select_related('match').order_by('-created_at')[:20]
            accurate = 0
            for ctx in recent_matches:
                if not ctx.match_id: continue
                comm_avg = PlayerEvaluation.objects.filter(match_id=ctx.match_id).aggregate(avg=Avg('contribution'))['avg']
                user_avg = PlayerEvaluation.objects.filter(user=user, match_id=ctx.match_id).aggregate(avg=Avg('contribution'))['avg']
                if comm_avg and user_avg and abs(user_avg - comm_avg) <= 1.0:
                    accurate += 1
            if accurate >= 16:
                b, created = UserBadge.objects.get_or_create(user=user, badge_type='accurate_analyst')
                if created: awarded.append(b)

        # 5. Без предвзятости (разница оценок своей/чужой команды <= 4 в 80% матчей)
        if total >= 15:
            contexts = ContextEvaluation.objects.filter(user=user, supported_team__isnull=False).select_related('match').order_by('-created_at')[:15]
            unbiased = 0
            checked = 0
            for ctx in contexts:
                supported = ctx.supported_team
                if not supported or not ctx.match_id: continue
                checked += 1
                own_avg = PlayerEvaluation.objects.filter(user=user, match_id=ctx.match_id, player__team=supported).aggregate(avg=Avg('contribution'))['avg'] or 0
                opponent = ctx.match.away_team if ctx.match.home_team == supported else ctx.match.home_team
                opp_avg = PlayerEvaluation.objects.filter(user=user, match_id=ctx.match_id, player__team=opponent).aggregate(avg=Avg('contribution'))['avg'] or 0
                if abs(own_avg - opp_avg) <= 4:
                    unbiased += 1
            if checked >= 10 and unbiased >= 8:
                b, created = UserBadge.objects.get_or_create(user=user, badge_type='bias_free')
                if created: awarded.append(b)

        # 6. Ранняя пташка (оценка в первые 2 часа после матча ≥ 5 раз)
        if total >= 5:
            early_count = ContextEvaluation.objects.filter(
                user=user, match__end_time__isnull=False,
                created_at__lte=F('match__end_time') + timedelta(hours=2)
            ).count()
            if early_count >= 5:
                b, created = UserBadge.objects.get_or_create(user=user, badge_type='early_bird')
                if created: awarded.append(b)

        # 7. Серия 7 дней
        if streak >= 7:
            b, created = UserBadge.objects.get_or_create(user=user, badge_type='streak_7')
            if created: awarded.append(b)

        # 8. Серия 30 дней
        if streak >= 30:
            b, created = UserBadge.objects.get_or_create(user=user, badge_type='streak_30')
            if created: awarded.append(b)

    except Exception as e:
        logger.error(f"Ошибка проверки достижений для {user.username}: {e}", exc_info=True)

    return awarded