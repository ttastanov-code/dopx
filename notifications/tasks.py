# notifications/tasks.py
from celery import shared_task
from django.utils import timezone
from django.conf import settings
from django.template.loader import render_to_string
from django.core.mail import EmailMultiAlternatives
import logging

logger = logging.getLogger(__name__)


def _send_email_to_user(user, subject, template_name, context, force=False):
    """
    Безопасная отправка email.
    force=True: игнорирует настройки пользователя (для верификации, сброса пароля и т.д.)
    """
    if not user or not user.email:
        logger.warning(f"⚠️ Cannot send email: user or email is missing")
        return False

    # Проверка настроек, если не форсируем
    if not force:
        try:
            # Получаем актуальные настройки
            notif_prefs = user.notification_settings
            subject_lower = subject.lower()
            
            # Маппинг типов уведомлений на ключи настроек
            if 'достижение' in subject_lower or 'badge' in subject_lower:
                if not notif_prefs.get('email_new_badge', True): return False
            elif 'уровень' in subject_lower or 'level' in subject_lower:
                if not notif_prefs.get('email_level_up', True): return False
            elif 'матч' in subject_lower or 'match' in subject_lower or 'голосование' in subject_lower:
                if not notif_prefs.get('email_match_finished', True): return False
            else:
                # Системные/прочие
                if not notif_prefs.get('email_system', True): return False
        except Exception as e:
            logger.error(f"❌ Error checking notification settings: {e}")
            return False

    backend = getattr(settings, 'EMAIL_BACKEND', '')
    host_user = getattr(settings, 'EMAIL_HOST_USER', None)
    
    # Для разработки выводим в консоль, если SMTP не настроен
    if backend.endswith('console.EmailBackend') or not host_user:
        logger.info(f"[EMAIL CONSOLE] To: {user.email} | Subject: {subject}")
        return True

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
        
        logger.info(f"✅ Email sent successfully to {user.email}: {subject}")
        return True
    except Exception as e:
        logger.error(f"❌ Failed to send email to {user.email}: {type(e).__name__}: {e}")
        return False


@shared_task(bind=True, max_retries=3, countdown=10)
def send_badge_earned_notification(self, user_id: str, badge_type: str, badge_name: str):
    """Отправка уведомления о получении достижения"""
    try:
        from users.models import User
        user = User.objects.get(id=user_id)
        logger.info(f"📤 Processing badge email for {user.username}: {badge_name}")
        _send_email_to_user(user, f'🎖️ Новое достижение: {badge_name}', 'emails/badge_earned.html', {'badge_name': badge_name})
        return True
    except User.DoesNotExist:
        logger.error(f"❌ User {user_id} not found for badge notification")
        return False
    except Exception as e:
        logger.error(f"❌ Error in send_badge_earned_notification: {e}", exc_info=True)
        raise self.retry(exc=e, countdown=60)


@shared_task(bind=True, max_retries=3, countdown=10)
def send_level_up_notification(self, user_id: str, new_level: int, total_xp: int):
    """Отправка уведомления о повышении уровня"""
    try:
        from users.models import User
        user = User.objects.get(id=user_id)
        logger.info(f"📤 Processing level up email for {user.username}: Level {new_level}")
        _send_email_to_user(user, f'⬆️ Вы достигли уровня {new_level}!', 'emails/level_up.html', {'new_level': new_level, 'total_xp': total_xp})
        return True
    except User.DoesNotExist: return False
    except Exception as e:
        logger.error(f"❌ Error in send_level_up_notification: {e}", exc_info=True)
        raise self.retry(exc=e, countdown=60)


@shared_task(bind=True, max_retries=3, countdown=5)
def send_email_verification(self, user_id: str, token: str):
    """Критическое письмо верификации (force=True)"""
    try:
        from users.models import User
        user = User.objects.get(id=user_id)
        site_url = getattr(settings, 'SITE_URL', 'https://dopx.kz')
        verify_url = f"{site_url}/users/verify-email/{token}/"
        
        _send_email_to_user(user, '👋 Подтвердите email на DOPX', 'emails/verify_email.html', {'verify_url': verify_url}, force=True)
        return True
    except Exception as e:
        logger.error(f"❌ Error in send_email_verification: {e}", exc_info=True)
        raise self.retry(exc=e, countdown=60)


@shared_task(bind=True, max_retries=3, countdown=5)
def send_voting_open_notification(self, match_id: str):
    """Оповещение о завершении матча и открытии голосования"""
    try:
        from matches.models import Match
        from users.models import User
        match = Match.objects.get(id=match_id)
        # Отправляем только тем, кто включил уведомления о матчах
        users = User.objects.filter(is_verified=True, email__isnull=False)
        count = 0
        for user in users:
            if _send_email_to_user(user, f'🏁 Матч завершён: {match.home_team.name} vs {match.away_team.name}', 'emails/voting_open.html', {'match': match}):
                count += 1
        logger.info(f"✅ Sent voting open emails to {count} users for match {match_id}")
        return True
    except Exception as e:
        logger.error(f"❌ Error in send_voting_open_notification: {e}", exc_info=True)
        raise self.retry(exc=e, countdown=60)