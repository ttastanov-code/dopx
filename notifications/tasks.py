# notifications/tasks.py
from celery import shared_task
from django.utils import timezone
from datetime import timedelta
from django.conf import settings
from django.template.loader import render_to_string
from django.core.mail import send_mail, EmailMultiAlternatives
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
# === ЗАДАЧИ ДЛЯ ДОСТИЖЕНИЙ И УРОВНЕЙ ===
# ============================================================================

@shared_task(bind=True, max_retries=3)
def send_badge_earned_notification(self, user_id: str, badge_type: str, badge_name: str):
    """
    Отправка уведомления о получении достижения
    ✅ ИСПРАВЛЕНО: Создаёт in-app уведомление + email
    """
    from users.models import User
    from notifications.models import Notification
    
    try:
        user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        logger.error(f"User {user_id} not found for badge notification")
        return False
    
    # ✅ ПРОВЕРКА НАСТРОЕК: используем метод модели
    if not user.get_notification_setting('email_system', True):
        logger.info(f"Email notifications disabled for user {user_id}")
        # Всё равно создаём in-app уведомление
    else:
        # Создаём in-app уведомление
        Notification.objects.create(
            user=user,
            notification_type='new_badge',
            title='🎖️ Новое достижение!',
            message=f'Вы получили достижение: {badge_name}',
            action_url='/users/profile/',
            is_read=False,
        )
        logger.info(f"In-app notification created for badge: {badge_name}")
        
        # ✅ ОТПРАВКА EMAIL если включено и пользователь верифицирован
        if user.is_verified and user.email:
            _send_badge_email.delay(user_id, badge_type, badge_name)
    
    logger.info(f"Badge notification processed for {user.username}: {badge_name}")
    return True


@shared_task(bind=True, max_retries=3)
def _send_badge_email(self, user_id: str, badge_type: str, badge_name: str):
    """Внутренняя задача: отправка email о достижении"""
    from users.models import User
    from django.core.mail import EmailMultiAlternatives
    from django.template.loader import render_to_string
    from django.conf import settings
    
    try:
        user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        return False
    
    if not user.email or not user.is_verified:
        return False
    
    site_url = getattr(settings, 'SITE_URL', 'https://dopx.kz')
    
    html_message = render_to_string('emails/badge_earned.html', {
        'user': user,
        'badge_name': badge_name,
        'badge_type': badge_type,
        'site_url': site_url,
        'site_name': 'DOPX',
    })
    
    email = EmailMultiAlternatives(
        subject=f'🎖️ Новое достижение: {badge_name} | DOPX',
        body='',  # Текстовая версия опционально
        from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@dopx.kz'),
        to=[user.email],
    )
    email.attach_alternative(html_message, "text/html")
    
    try:
        email.send(fail_silently=False)
        logger.info(f"✅ Badge email sent to {user.email}")
    except Exception as e:
        logger.error(f"❌ Failed to send badge email: {e}")
        # Не поднимаем исключение, чтобы не ретраить бесконечно
    
    return True


@shared_task(bind=True, max_retries=3)
def send_level_up_notification(self, user_id: str, new_level: int, total_xp: int):
    """Отправка уведомления о повышении уровня"""
    from users.models import User
    from notifications.models import Notification
    
    try:
        user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        logger.error(f"User {user_id} not found for level up notification")
        return False
    
    # ✅ ПРОВЕРКА НАСТРОЕК
    if not user.get_notification_setting('email_system', True):
        logger.info(f"Email notifications disabled for level up: {user_id}")
    else:
        # Создаём in-app уведомление
        Notification.objects.create(
            user=user,
            notification_type='level_up',
            title=f'⬆️ Новый уровень {new_level}!',
            message=f'Поздравляем! Вы достигли уровня {new_level} с {total_xp} XP. Продолжайте в том же духе!',
            action_url='/users/profile/',
            is_read=False,
        )
        logger.info(f"In-app notification created for level up: {new_level}")
        
        # ✅ ОТПРАВКА EMAIL
        if user.is_verified and user.email:
            _send_level_up_email.delay(user_id, new_level, total_xp)
    
    logger.info(f"Level up notification processed for {user.username}: level {new_level}")
    return True


@shared_task(bind=True, max_retries=3)
def _send_level_up_email(self, user_id: str, new_level: int, total_xp: int):
    """Внутренняя задача: отправка email о повышении уровня"""
    from users.models import User
    from django.core.mail import EmailMultiAlternatives
    from django.template.loader import render_to_string
    from django.conf import settings
    
    try:
        user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        return False
    
    if not user.email or not user.is_verified:
        return False
    
    site_url = getattr(settings, 'SITE_URL', 'https://dopx.kz')
    next_threshold = 100 * new_level
    
    html_message = render_to_string('emails/level_up.html', {
        'user': user,
        'new_level': new_level,
        'total_xp': total_xp,
        'next_threshold': next_threshold,
        'site_url': site_url,
        'site_name': 'DOPX',
    })
    
    email = EmailMultiAlternatives(
        subject=f'⬆️ Вы достигли уровня {new_level}! | DOPX',
        body='',
        from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@dopx.kz'),
        to=[user.email],
    )
    email.attach_alternative(html_message, "text/html")
    
    try:
        email.send(fail_silently=False)
        logger.info(f"✅ Level up email sent to {user.email}")
    except Exception as e:
        logger.error(f"❌ Failed to send level up email: {e}")
    
    return True


@shared_task(bind=True, max_retries=3)
def send_trust_score_updated_notification(self, user_id: str, old_score: float, new_score: float):
    """Отправка уведомления об изменении Trust Score"""
    from users.models import User
    from notifications.models import Notification
    
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

# === ДОБАВЬТЕ В КОНЕЦ ФАЙЛА: notifications/tasks.py ===

@shared_task(bind=True, max_retries=3)
def send_badge_earned_notification(self, user_id: str, badge_type: str, badge_name: str):
    """Отправка уведомления о получении достижения"""
    from users.models import User
    from notifications.models import Notification
    from django.utils import timezone
    from datetime import timedelta
    
    try:
        user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        logger.error(f"User {user_id} not found for badge notification")
        return False
    
    # ✅ Дедупликация: не спамить одинаковыми уведомлениями в течение часа
    if Notification.objects.filter(
        user=user,
        notification_type='new_badge',
        created_at__gte=timezone.now() - timedelta(hours=1)
    ).exists():
        logger.debug(f"Badge notification recently sent to {user.username}")
        return True
    
    # Создаём in-app уведомление
    Notification.objects.create(
        user=user,
        notification_type='new_badge',
        title='🎖️ Новое достижение!',
        message=f'Вы получили достижение: {badge_name}',
        action_url='/users/profile/',
    )
    
    # ✅ Проверка настроек через метод модели
    if not user.get_notification_setting('email_system', True):
        logger.info(f"Email notifications disabled for user {user.username}")
        return True
    
    # Отправляем email если пользователь верифицирован
    if user.is_verified and user.email:
        _send_badge_email.delay(user_id, badge_type, badge_name)
    
    logger.info(f"Badge notification sent to {user.username}: {badge_name}")
    return True


@shared_task(bind=True, max_retries=3)
def _send_badge_email(self, user_id: str, badge_type: str, badge_name: str):
    """Внутренняя задача: отправка email о достижении"""
    from users.models import User
    from django.core.mail import EmailMultiAlternatives
    from django.template.loader import render_to_string
    from django.conf import settings
    
    try:
        user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        return False
    
    if not user.email or not user.is_verified:
        return False
    
    # ✅ Проверка настроек email
    if not getattr(settings, 'EMAIL_HOST_USER', None):
        logger.warning(f"Email not configured: EMAIL_HOST_USER is empty. Badge {badge_name} for {user.email}")
        return False
    
    site_url = getattr(settings, 'SITE_URL', 'https://dopx.kz')
    
    try:
        html_message = render_to_string('emails/badge_earned.html', {
            'user': user,
            'badge_name': badge_name,
            'badge_type': badge_type,
            'site_url': site_url,
            'site_name': 'DOPX',
        })
        
        email = EmailMultiAlternatives(
            subject=f'🎖️ Новое достижение: {badge_name} | DOPX',
            body='',
            from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@dopx.kz'),
            to=[user.email],
        )
        email.attach_alternative(html_message, "text/html")
        
        # ✅ Отправляем с fail_silently=False для отладки, но с обработкой ошибок
        email.send(fail_silently=False)
        logger.info(f"✅ Badge email sent to {user.email}: {badge_name}")
        
    except Exception as e:
        logger.error(f"❌ Failed to send badge email to {user.email}: {type(e).__name__}: {e}")
        # Не поднимаем исключение, чтобы не ломать основной поток
        return False
    
    return True


@shared_task(bind=True, max_retries=3)
def send_level_up_notification(self, user_id: str, new_level: int, total_xp: int):
    """Отправка уведомления о повышении уровня"""
    from users.models import User
    from notifications.models import Notification
    from django.conf import settings
    
    try:
        user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        logger.error(f"User {user_id} not found for level up notification")
        return False
    
    # ✅ ДЕДУПЛИКАЦИЯ: проверяем, не было ли уже уведомления об этом уровне
    if Notification.objects.filter(
        user=user,
        notification_type='level_up',
        message__icontains=f'уровня {new_level}'
    ).exists():
        logger.debug(f"Level up notification already exists for {user.username}: level {new_level}")
        return True
    
    # In-app уведомление
    Notification.objects.create(
        user=user,
        notification_type='level_up',
        title=f'⬆️ Новый уровень {new_level}!',
        message=f'Поздравляем! Вы достигли уровня {new_level} с {total_xp} XP. Продолжайте в том же духе!',
        action_url='/users/profile/',
    )
    
    # Email если включено
    user_settings = getattr(user, 'notification_settings', {})
    if isinstance(user_settings, str):
        import json
        try:
            user_settings = json.loads(user_settings)
        except:
            user_settings = {}
    
    if user.is_verified and user.email and user_settings.get('email_system', True):
        _send_level_up_email.delay(user_id, new_level, total_xp)
    
    logger.info(f"Level up notification sent to {user.username}: level {new_level}")
    return True


@shared_task(bind=True, max_retries=3)
def _send_level_up_email(self, user_id: str, new_level: int, total_xp: int):
    """Внутренняя задача: отправка email о повышении уровня"""
    from users.models import User
    from django.core.mail import EmailMultiAlternatives
    from django.template.loader import render_to_string
    from django.conf import settings
    
    try:
        user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        return False
    
    if not user.email or not user.is_verified:
        return False
    
    # ✅ Проверка настроек email
    if not getattr(settings, 'EMAIL_HOST_USER', None):
        logger.warning(f"Email not configured: EMAIL_HOST_USER is empty. Level up {new_level} for {user.email}")
        return False
    
    site_url = getattr(settings, 'SITE_URL', 'https://dopx.kz')
    
    try:
        html_message = render_to_string('emails/level_up.html', {
            'user': user,
            'new_level': new_level,
            'total_xp': total_xp,
            'next_threshold': 100 * new_level,
            'site_url': site_url,
            'site_name': 'DOPX',
        })
        
        email = EmailMultiAlternatives(
            subject=f'⬆️ Вы достигли уровня {new_level}! | DOPX',
            body='',
            from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@dopx.kz'),
            to=[user.email],
        )
        email.attach_alternative(html_message, "text/html")
        
        email.send(fail_silently=False)
        logger.info(f"✅ Level up email sent to {user.email}: level {new_level}")
        
    except Exception as e:
        logger.error(f"❌ Failed to send level up email to {user.email}: {type(e).__name__}: {e}")
        return False
    
    return True