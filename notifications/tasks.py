# notifications/tasks.py — ПОЛНАЯ ВЕРСИЯ (гибридная)
from celery import shared_task
from django.utils import timezone
from datetime import timedelta
from django.conf import settings
from django.template.loader import render_to_string
from django.core.mail import send_mail, EmailMultiAlternatives
from django.db.models import Q, Count
import logging
from notifications.models import Notification
from users.models import User
from matches.models import Match

logger = logging.getLogger(__name__)

# ============================================================================
# === КОНФИГУРАЦИЯ УВЕДОМЛЕНИЙ ===
# ============================================================================
NOTIFICATION_TITLES = {
    'welcome': '👋 Добро пожаловать!',
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
    'welcome': 'Добро пожаловать в DOPX! Начните оценивать матчи.',
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
# === ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ ДЛЯ ОТПРАВКИ EMAIL ===
# ============================================================================
def _send_email_to_user(user, subject, template_name, context):
    """
    Универсальная функция для отправки HTML email с проверками.
    Возвращает True при успехе, False при ошибке (не поднимает исключение).
    """
    if not user.email or not user.is_verified:
        return False

    # ✅ Проверка: настроен ли email в проекте
    if not getattr(settings, 'EMAIL_HOST_USER', None):
        logger.warning(f"Email not configured: EMAIL_HOST_USER is empty. Skipping email to {user.email}")
        return False

    try:
        html_message = render_to_string(template_name, {
            'user': user,
            'site_url': getattr(settings, 'SITE_URL', 'https://dopx.kz'),
            'site_name': 'DOPX',
            **context
        })
        email = EmailMultiAlternatives(
            subject=subject,
            body='',
            from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@dopx.kz'),
            to=[user.email],
        )
        email.attach_alternative(html_message, "text/html")
        email.send(fail_silently=False)
        return True
    except Exception as e:
        logger.error(f"Failed to send email to {user.email}: {type(e).__name__}: {e}")
        return False  # ✅ Не поднимаем исключение — не ломаем поток


# ============================================================================
# === НОВЫЕ ЗАДАЧИ ===
# ============================================================================

@shared_task(bind=True, max_retries=3)
def send_welcome_notification(self, user_id: str):
    """
    1. Приветственное письмо после регистрации
    ✅ ОТПРАВЛЯЕТСЯ ВСЕМ ПО УМОЛЧАНИЮ (не проверяет настройки)
    """
    try:
        user = User.objects.get(id=user_id)
        
        # ✅ Создаём in-app уведомление (всегда)
        Notification.objects.create(
            user=user,
            notification_type='welcome',
            title='👋 Добро пожаловать в DOPX!',
            message='Спасибо за регистрацию! Теперь вы можете оценивать матчи, получать достижения и подниматься в рейтинге.',
            action_url='/matches/',
            is_read=False,
        )
        
        # ✅ Отправляем email (всегда, если пользователь верифицирован)
        if user.is_verified and user.email:
            _send_email_to_user(user, 'Добро пожаловать в DOPX!', 'emails/welcome.html', {})
            logger.info(f"Welcome email sent to {user.username}")
        
        logger.info(f"Welcome notification processed for {user.username}")
        return True
    except User.DoesNotExist:
        logger.error(f"User {user_id} not found for welcome notification")
        return False
    except Exception as e:
        logger.error(f"Error in send_welcome_notification: {e}", exc_info=True)
        raise self.retry(exc=e, countdown=60)


@shared_task(bind=True, max_retries=3)
def send_voting_open_notification(self, match_id: str):
    """
    2. Матч завершён / Голосование открыто
    ✅ С проверкой настроек пользователя (можно отключить)
    """
    try:
        match = Match.objects.get(id=match_id)
        
        # Отправляем всем верифицированным активным пользователям
        users = User.objects.filter(is_verified=True, is_active=True)
        
        for user in users:
            # ✅ Проверка настроек пользователя (можно отключить)
            if not user.get_notification_setting('email_match_finished', True):
                continue
            
            # Дедупликация: не отправлять дубли в течение 1 часа
            if Notification.objects.filter(
                user=user,
                notification_type='match_finished',
                related_match=match,
                created_at__gte=timezone.now() - timedelta(hours=1)
            ).exists():
                continue
            
            # In-app уведомление
            Notification.objects.create(
                user=user,
                notification_type='match_finished',
                title='🏁 Матч завершён: Голосование открыто!',
                message=f'Матч {match.home_team.name} vs {match.away_team.name} завершён. Успейте оценить игру!',
                action_url=f'/evaluations/match/{match.id}/context/',
                related_match=match,
                is_read=False,
            )
            
            # Email
            _send_email_to_user(
                user, 
                f'Матч завершён: {match.home_team.name} vs {match.away_team.name}', 
                'emails/voting_open.html', 
                {'match': match}
            )
        
        logger.info(f"Voting open notifications sent for match {match_id}")
        return True
    except Match.DoesNotExist:
        logger.error(f"Match {match_id} not found")
        return False
    except Exception as e:
        logger.error(f"Error in send_voting_open_notification: {e}", exc_info=True)
        raise self.retry(exc=e, countdown=60)


@shared_task
def notify_voting_closing_soon():
    """
    3. Напоминание когда голосование скоро закроется (за час минимум)
    ✅ С проверкой настроек пользователя
    """
    now = timezone.now()
    # Ищем матчи, где голосование закроется в ближайший час (55-65 минут)
    deadline_min = now + timedelta(minutes=55)
    deadline_max = now + timedelta(minutes=65)
    
    matches = Match.objects.filter(
        status='finished',
        voting_open_until__gte=deadline_min,
        voting_open_until__lte=deadline_max
    )
    
    count = 0
    for match in matches:
        users = User.objects.filter(is_verified=True, is_active=True)
        for user in users:
            # ✅ Проверка настроек пользователя (можно отключить)
            if not user.get_notification_setting('email_voting_closing', True):
                continue

            # Дедупликация
            if Notification.objects.filter(
                user=user,
                notification_type='voting_closing',
                related_match=match,
                created_at__gte=timezone.now() - timedelta(hours=2)
            ).exists():
                continue

            Notification.objects.create(
                user=user,
                notification_type='voting_closing',
                title='⏰ Голосование скоро закроется!',
                message=f'Осталось около часа, чтобы оценить матч {match.home_team.name} vs {match.away_team.name}.',
                action_url=f'/evaluations/match/{match.id}/context/',
                related_match=match,
                is_read=False,
            )
            _send_email_to_user(
                user,
                f'Напоминание: голосование за матч {match.home_team.name} закрывается',
                'emails/voting_closing.html',
                {'match': match}
            )
            count += 1
            
    logger.info(f"Sent {count} voting closing reminders")
    return f"Sent {count} closing reminders"


@shared_task(bind=True, max_retries=3)
def send_system_notification_to_all(self, title: str, message: str, action_url: str = '/core/'):
    """
    6. Системные уведомления (рассылка всем, с проверкой настроек)
    """
    users = User.objects.filter(is_active=True)
    count = 0
    
    for user in users:
        # ✅ Системные уведомления можно отключить (кроме критических)
        if not user.get_notification_setting('email_system', True):
            continue
        
        Notification.objects.create(
            user=user,
            notification_type='system',
            title=title,
            message=message,
            action_url=action_url,
            is_read=False,
        )
        count += 1
        
    logger.info(f"Sent system notification to {count} users")
    return f"Sent system notification to {count} users"


# ============================================================================
# === УЛУЧШЕННЫЕ СУЩЕСТВУЮЩИЕ ЗАДАЧИ ===
# ============================================================================

@shared_task(bind=True, max_retries=3)
def send_badge_earned_notification(self, user_id: str, badge_type: str, badge_name: str):
    """
    4. Новые Достижения (с проверкой настроек и дедупликацией)
    ✅ ИСПРАВЛЕНО: Создаёт in-app уведомление + email
    """
    try:
        user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        logger.error(f"User {user_id} not found for badge notification")
        return False
    
    # ✅ Проверка настроек пользователя (можно отключить)
    if not user.get_notification_setting('email_new_badge', True):
        logger.info(f"Badge email disabled for user {user.username}")
        return True  # Не ошибка, просто пользователь отключил
    
    # ✅ Дедупликация: не спамить одинаковыми уведомлениями в течение часа
    if Notification.objects.filter(
        user=user,
        notification_type='new_badge',
        message__icontains=badge_name,
        created_at__gte=timezone.now() - timedelta(hours=1)
    ).exists():
        logger.debug(f"Badge notification recently sent to {user.username}: {badge_name}")
        return True

    # Создаём in-app уведомление
    Notification.objects.create(
        user=user,
        notification_type='new_badge',
        title='🎖️ Новое достижение!',
        message=f'Вы получили достижение: {badge_name}',
        action_url='/users/profile/',
        is_read=False,
    )
    
    # Отправляем email если пользователь верифицирован
    if user.is_verified and user.email:
        _send_email_to_user(
            user,
            f'Новое достижение: {badge_name}',
            'emails/badge_earned.html',
            {'badge_name': badge_name}
        )
    
    logger.info(f"Badge notification sent to {user.username}: {badge_name}")
    return True


@shared_task(bind=True, max_retries=3)
def send_level_up_notification(self, user_id: str, new_level: int, total_xp: int):
    """
    5. Повышение уровня пользователя (с проверкой настроек и дедупликацией)
    """
    try:
        user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        logger.error(f"User {user_id} not found for level up notification")
        return False
    
    # ✅ Проверка настроек пользователя (можно отключить)
    if not user.get_notification_setting('email_level_up', True):
        logger.info(f"Level up email disabled for user {user.username}")
        return True
    
    # ✅ Дедупликация: проверяем, не было ли уже уведомления об этом уровне
    if Notification.objects.filter(
        user=user,
        notification_type='level_up',
        message__icontains=f'уровня {new_level}',
        created_at__gte=timezone.now() - timedelta(hours=1)
    ).exists():
        logger.debug(f"Level up notification already exists for {user.username}: level {new_level}")
        return True

    # In-app уведомление
    Notification.objects.create(
        user=user,
        notification_type='level_up',
        title=f'⬆️ Новый уровень {new_level}!',
        message=f'Поздравляем! Вы достигли уровня {new_level} с {total_xp} XP.',
        action_url='/users/profile/',
        is_read=False,
    )
    
    # Email
    if user.is_verified and user.email:
        _send_email_to_user(
            user,
            f'Вы достигли уровня {new_level}!',
            'emails/level_up.html',
            {'new_level': new_level, 'total_xp': total_xp}
        )
    
    logger.info(f"Level up notification sent to {user.username}: level {new_level}")
    return True


@shared_task(bind=True, max_retries=3)
def send_trust_score_updated_notification(self, user_id: str, old_score: float, new_score: float):
    """Отправка уведомления об изменении Trust Score"""
    try:
        user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        return False
    
    # Определяем уровень доверия
    def get_trust_label(score):
        if score >= 1.8:
            return 'Эксперт', 'success'
        elif score >= 1.4:
            return 'Надёжный', 'warning'
        elif score >= 1.0:
            return 'Стандартный', 'info'
        else:
            return 'Новичок', 'error'
    
    old_label, old_color = get_trust_label(old_score)
    new_label, new_color = get_trust_label(new_score)
    
    # Только если уровень изменился
    if old_label != new_label:
        Notification.objects.create(
            user=user,
            notification_type='system',
            title=f'🛡️ Ваш Trust Score обновлён',
            message=f'Ваш уровень доверия: {old_label} → {new_label} ({new_score})',
            action_url='/users/profile/',
            is_read=False,
        )
        logger.info(f"Trust score notification created for {user.username}")
    
    return True


# ============================================================================
# === УТИЛИТЫ: ОЧИСТКА И ОБСЛУЖИВАНИЕ (без изменений) ===
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
        
        # Проверяем настройки уведомлений
        if not user.get_notification_setting('email_system', True):
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