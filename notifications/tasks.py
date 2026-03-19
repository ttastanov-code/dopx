# notifications/tasks.py — ПОЛНАЯ ЗАМЕНА

from celery import shared_task
from django.utils import timezone
from datetime import timedelta
from matches.models import Match
from evaluations.models import EvaluationSession
from notifications.models import Notification
from users.models import User
import logging

logger = logging.getLogger(__name__)


@shared_task
def send_match_finished_notifications(match_id):
    """Уведомить пользователей о завершении матча"""
    from uuid import UUID
    try:
        match = Match.objects.get(id=UUID(match_id))
    except (ValueError, Match.DoesNotExist):
        return False
    
    # Находим пользователей, которые начали оценку но не завершили
    sessions = EvaluationSession.objects.filter(
        match=match,
        status='in_progress'
    ).select_related('user')
    
    count = 0
    for session in sessions:
        Notification.objects.create(
            user=session.user,
            notification_type='voting_closing',
            title=f'Завершите оценку: {match.home_team} vs {match.away_team}',
            message='Голосование закроется через 24 часа',
            action_url=f'/evaluations/match/{match_id}/context/',
            related_match=match,
        )
        count += 1
    
    logger.info(f"Sent {count} match finished notifications")
    return count


@shared_task
def send_voting_open_notifications(match_id):
    """Уведомить о открытии голосования"""
    from uuid import UUID
    try:
        match = Match.objects.get(id=UUID(match_id))
    except (ValueError, Match.DoesNotExist):
        return False
    
    # Все активные пользователи
    users = User.objects.filter(is_active=True)[:1000]
    
    notifications = []
    for user in users:
        notifications.append(Notification(
            user=user,
            notification_type='voting_open',
            title=f'Новый матч для оценки: {match.home_team} vs {match.away_team}',
            message='Голосование открыто на 48 часов',
            action_url=f'/matches/{match_id}/',
            related_match=match,
        ))
    
    Notification.objects.bulk_create(notifications[:500])  # Лимит 500
    logger.info(f"Sent {len(notifications[:500])} voting open notifications")
    return len(notifications[:500])


@shared_task
def send_top_player_notifications(match_id, player_id):
    """Уведомить если игрок пользователя в топе"""
    from uuid import UUID
    from players.models import Player
    from aggregates.models import PlayerMatchAggregate
    
    try:
        match = Match.objects.get(id=UUID(match_id))
        player = Player.objects.get(id=UUID(player_id))
    except (ValueError, Match.DoesNotExist, Player.DoesNotExist):
        return False
    
    # Проверяем если игрок в топ-3
    top_players = PlayerMatchAggregate.objects.filter(
        match=match
    ).order_by('-performance_score')[:3]
    
    if player not in [p.player for p in top_players]:
        return False
    
    # Находим пользователей которые оценили этого игрока
    from evaluations.models import PlayerEvaluation
    eval_users = PlayerEvaluation.objects.filter(
        match=match, player=player
    ).values_list('user_id', flat=True).distinct()
    
    notifications = []
    for user_id in eval_users:
        notifications.append(Notification(
            user_id=user_id,
            notification_type='top_performance',
            title=f'Ваш игрок в топе!',
            message=f'{player} вошёл в топ-3 матча',
            action_url=f'/players/{player_id}/',
            related_match=match,
        ))
    
    Notification.objects.bulk_create(notifications)
    logger.info(f"Sent {len(notifications)} top player notifications")
    return len(notifications)


@shared_task
def cleanup_old_notifications():
    """Очистка старых уведомлений"""
    from django.utils import timezone
    from datetime import timedelta
    
    cutoff = timezone.now() - timedelta(days=30)
    deleted, _ = Notification.objects.filter(
        is_read=True,
        created_at__lt=cutoff
    ).delete()
    
    logger.info(f"Cleaned up {deleted} old notifications")
    return deleted