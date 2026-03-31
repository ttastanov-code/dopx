# notifications/tasks.py
from celery import shared_task
from django.utils import timezone
from datetime import timedelta
from django.conf import settings
from django.template.loader import render_to_string
from django.core.mail import send_mail
from django.db.models import Q
import logging
from notifications.models import Notification
from users.models import User
from matches.models import Match

logger = logging.getLogger(__name__)

# ============================================================================
# === КОНФИГУРАЦИЯ УВЕДОМЛЕНИЙ ===
# ============================================================================
NOTIFICATION_TITLES = {
    'match_finished': '🏁 Матч завершён',
    'voting_open': '🎯 Голосование открыто',
    'voting_closing': '⏰ Голосование закрывается',
    'aggregate_updated': '📊 Рейтинги обновлены',
    'top_performance': '🏆 Ваш игрок в топ-3!',
    'verification_required': '🔐 Требуется верификация',
    'new_badge': '🎖️ Новое достижение!',
    'level_up': '⬆️ Новый уровень!',
    'system': '📢 Системное уведомление',
}

NOTIFICATION_MESSAGES = {
    'match_finished': 'Матч {match} завершён. Успейте оценить, пока открыто голосование!',
    'voting_open': 'Голосование за матч {match} открыто. Оцените и получите XP!',
    'voting_closing': 'Голосование за матч {match} закроется через 24 часа.',
    'aggregate_updated': 'Рейтинги игроков и команд обновлены по итогам матча {match}.',
    'top_performance': '{player} вошёл в топ-3 лучших игроков матча {match}!',
    'verification_required': 'Подтвердите ваш email для полного доступа к платформе.',
    'new_badge': 'Вы получили новое достижение! Проверьте профиль.',
    'level_up': 'Поздравляем! Вы достигли уровня {level}. Продолжайте в том же духе!',
    'system': 'Важное обновление платформы. Проверьте информацию в личном кабинете.',
}


# ============================================================================
# === ЗАДАЧИ ДЛЯ МАТЧЕЙ И ОЦЕНОК ===
# ============================================================================
@shared_task(bind=True, max_retries=3)
def send_match_finished_notifications(self, match_id: str):
    """Уведомить пользователей о завершении матча"""
    from uuid import UUID
    from matches.models import Match
    from evaluations.models import EvaluationSession
    
    try:
        match = Match.objects.get(id=UUID(match_id))
    except (ValueError, Match.DoesNotExist):
        logger.error(f"Match {match_id} not found")
        return 0
    
    sessions = EvaluationSession.objects.filter(
        match=match,
        status='in_progress'
    ).select_related('user')
    
    count = 0
    match_name = f"{match.home_team.name} vs {match.away_team.name}"
    
    for session in sessions:
        title = NOTIFICATION_TITLES.get('match_finished', '🏁 Матч завершён')
        message = NOTIFICATION_MESSAGES['match_finished'].format(match=match_name)
        
        Notification.objects.create(
            user=session.user,
            notification_type='match_finished',
            title=title,
            message=message,
            action_url=f'/evaluations/match/{match_id}/context/',
            related_match=match,
        )
        count += 1
    
    logger.info(f"Sent {count} match finished notifications for match {match_id}")
    return count


@shared_task(bind=True, max_retries=3)
def send_voting_open_notifications(self, match_id: str):
    """Уведомить об открытии голосования"""
    from uuid import UUID
    from matches.models import Match
    
    try:
        match = Match.objects.get(id=UUID(match_id))
    except (ValueError, Match.DoesNotExist):
        logger.error(f"Match {match_id} not found")
        return 0
    
    users = User.objects.filter(
        is_active=True,
        is_verified=True
    )[:500]
    
    notifications = []
    match_name = f"{match.home_team.name} vs {match.away_team.name}"
    
    for user in users:
        title = NOTIFICATION_TITLES.get('voting_open', '🎯 Голосование открыто')
        message = NOTIFICATION_MESSAGES['voting_open'].format(match=match_name)
        
        notifications.append(Notification(
            user=user,
            notification_type='voting_open',
            title=title,
            message=message,
            action_url=f'/matches/{match_id}/',
            related_match=match,
        ))
    
    Notification.objects.bulk_create(notifications)
    logger.info(f"Sent {len(notifications)} voting open notifications for match {match_id}")
    return len(notifications)


@shared_task(bind=True, max_retries=3)
def send_top_player_notifications(self, match_id: str, player_id: str):
    """Уведомить если игрок пользователя в топе"""
    from uuid import UUID
    from players.models import Player
    from aggregates.models import PlayerMatchAggregate
    from evaluations.models import PlayerEvaluation
    
    try:
        match = Match.objects.get(id=UUID(match_id))
        player = Player.objects.get(id=UUID(player_id))
    except (ValueError, Match.DoesNotExist, Player.DoesNotExist):
        logger.error(f"Match {match_id} or Player {player_id} not found")
        return 0
    
    top_players = PlayerMatchAggregate.objects.filter(
        match=match
    ).order_by('-performance_score')[:3]
    
    if player not in [p.player for p in top_players]:
        logger.debug(f"Player {player_id} not in top 3 for match {match_id}")
        return 0
    
    eval_users = PlayerEvaluation.objects.filter(
        match=match,
        player=player
    ).values_list('user_id', flat=True).distinct()
    
    notifications = []
    match_name = f"{match.home_team.name} vs {match.away_team.name}"
    player_name = f"{player.first_name} {player.last_name}"
    
    for user_id in eval_users:
        title = NOTIFICATION_TITLES.get('top_performance', '🏆 Ваш игрок в топ-3!')
        message = NOTIFICATION_MESSAGES['top_performance'].format(
            player=player_name,
            match=match_name
        )
        
        notifications.append(Notification(
            user_id=user_id,
            notification_type='top_performance',
            title=title,
            message=message,
            action_url=f'/players/{player_id}/',
            related_match=match,
        ))
    
    Notification.objects.bulk_create(notifications)
    logger.info(f"Sent {len(notifications)} top player notifications for match {match_id}")
    return len(notifications)


# ============================================================================
# === УТИЛИТЫ: ОЧИСТКА И ОБСЛУЖИВАНИЕ ===
# ============================================================================
@shared_task
def cleanup_old_notifications():
    """Очистка старых уведомлений (старше 30 дней)"""
    cutoff = timezone.now() - timedelta(days=30)
    deleted_count, _ = Notification.objects.filter(
        is_read=True,
        created_at__lt=cutoff
    ).delete()
    logger.info(f"Cleaned up {deleted_count} old notifications")
    return deleted_count


@shared_task
def cleanup_old_sessions():
    """Очистка старых сессий оценки (старше 7 дней)"""
    from evaluations.models import EvaluationSession
    
    cutoff = timezone.now() - timedelta(days=7)
    deleted_count, _ = EvaluationSession.objects.filter(
        status='started',
        created_at__lt=cutoff
    ).delete()
    logger.info(f"Cleaned up {deleted_count} old evaluation sessions")
    return deleted_count


@shared_task
def send_notification_digest():
    """Ежедневная рассылка дайджеста уведомлений"""
    from django.db.models import Count
    
    cutoff = timezone.now() - timedelta(hours=24)
    users_with_notifications = Notification.objects.filter(
        is_read=False,
        created_at__gte=cutoff
    ).values('user_id').annotate(
        count=Count('id')
    ).filter(count__gte=5)
    
    sent_count = 0
    for user_data in users_with_notifications:
        user = User.objects.get(id=user_data['user_id'])
        if not user.email or not user.is_verified:
            continue
        
        notifications = Notification.objects.filter(
            user=user,
            is_read=False,
            created_at__gte=cutoff
        ).order_by('-created_at')[:10]
        
        try:
            html = render_to_string('emails/notification_digest.html', {
                'user': user,
                'notifications': notifications,
                'count': user_data['count'],
                'site_name': 'DOPX',
                'site_url': getattr(settings, 'SITE_URL', 'https://dopx.kz'),
            })
            
            send_mail(
                subject=f'📬 Дайджест уведомлений ({user_data["count"]}) | DOPX',
                message='',
                from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@dopx.kz'),
                recipient_list=[user.email],
                html_message=html,
                fail_silently=False,
            )
            sent_count += 1
        except Exception as e:
            logger.error(f"Digest email error for user {user.id}: {e}")
    
    logger.info(f"Sent {sent_count} notification digests")
    return sent_count


@shared_task
def health_check():
    """Проверка работоспособности Celery"""
    from django.db import connection
    from django.core.cache import cache
    
    result = {
        'status': 'OK',
        'timestamp': timezone.now().isoformat(),
        'checks': {}
    }
    
    try:
        connection.ensure_connection()
        result['checks']['database'] = 'OK'
    except Exception as e:
        result['checks']['database'] = f'ERROR: {e}'
        result['status'] = 'DEGRADED'
    
    try:
        cache.set('health_check', 'OK', timeout=10)
        cache_value = cache.get('health_check')
        if cache_value == 'OK':
            result['checks']['cache'] = 'OK'
        else:
            result['checks']['cache'] = 'ERROR: Cache read failed'
            result['status'] = 'DEGRADED'
    except Exception as e:
        result['checks']['cache'] = f'ERROR: {e}'
        result['status'] = 'DEGRADED'
    
    logger.info(f"Health check: {result['status']}")
    return result