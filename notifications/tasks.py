# notifications/tasks.py
"""Celery-задачи уведомлений: email, push, in-app.

_send_email_to_user — единственная точка отправки письма пользователю: проверяет
настройки по notification_type и не шлёт тестовым ботам. Массовые рассылки идут
пачками (BULK_EMAIL_CHUNK_SIZE) через отдельные задачи. Пользователям с
email_digest_mode письма о бейджах/уровнях собираются в дайджест.
"""
from __future__ import annotations

import logging
import smtplib
import socket
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.core.cache import cache
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.html import strip_tags

logger = logging.getLogger(__name__)

# Временные сетевые ошибки, на которых есть смысл ретраить.
TRANSIENT_EMAIL_ERRORS = (
    socket.gaierror,
    ConnectionError,
    TimeoutError,
    smtplib.SMTPServerDisconnected,
    smtplib.SMTPConnectError,
    smtplib.SMTPHeloError,
)


class TransientEmailError(Exception):
    """Временный сбой отправки письма.

    Поднимается из _send_email_to_user при raise_on_transient=True, чтобы задача
    на одного получателя (или пачку) могла сделать retry. В циклах по многим
    пользователям не поднимается — там email_sent_at ставится только после
    успеха, и следующий прогон досылает.
    """

# Размер пачки при массовой рассылке.
BULK_EMAIL_CHUNK_SIZE = 50

# TTL Redis-лока для периодических задач (защита от двойного прогона).
NOTIFY_TASK_LOCK_TIMEOUT = 600

# Сколько дней хранить прочитанные уведомления.
NOTIFICATION_RETENTION_DAYS = 90

# Тип уведомления -> ключ настройки пользователя. None — только через force=True.
NOTIFICATION_TYPE_TO_SETTINGS_KEY: dict[str, str] = {
    "match_finished": "email_match_finished",
    "voting_open": "email_match_finished",
    "voting_closing": "email_voting_closing",
    "new_badge": "email_new_badge",
    "level_up": "email_level_up",
    "system": "email_system",
    "prediction_closing": "email_prediction_closing",
    "prediction_result": "email_prediction_result",
    "weekly_digest": "email_weekly_summary",
    "round_results": "email_round_results",
}

# Типы, которые при email_digest_mode уходят в дайджест.
DIGESTIBLE_NOTIFICATION_TYPES = ("new_badge", "level_up", "system")


def _send_email_to_user(
    user,
    subject: str,
    template_name: str,
    context: dict,
    notification_type: str | None = None,
    force: bool = False,
    raise_on_transient: bool = False,
) -> bool:
    """Отправка письма пользователю.

    :param notification_type: тип для проверки настроек (NOTIFICATION_TYPE_TO_SETTINGS_KEY).
    :param force: игнорировать настройки (верификация, сброс пароля).
    :param raise_on_transient: при сетевом сбое поднять TransientEmailError вместо False.
    """
    if not user or not user.email:
        logger.warning("⚠️ Cannot send email: user or email is missing")
        return False

    # Тестовым ботам не отправляем.
    from core.utils import is_synthetic_test_email

    if is_synthetic_test_email(user.email):
        logger.debug(f"_send_email_to_user: пропуск синтетического тестового аккаунта {user.email}")
        return False

    if not force:
        settings_key = NOTIFICATION_TYPE_TO_SETTINGS_KEY.get(notification_type or "", "email_system")
        try:
            if not user.get_notification_setting(settings_key, True):
                return False
        except Exception as e:
            logger.error(f"❌ Error checking notification settings: {e}")
            return False

    backend = getattr(settings, 'EMAIL_BACKEND', '')
    host_user = getattr(settings, 'EMAIL_HOST_USER', None)

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
            # text/plain-версия из HTML — письмо только с HTML похоже на спам.
            subject=subject,
            body=strip_tags(html_message),
            from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@dopx.kz'),
            to=[user.email],
        )
        email.attach_alternative(html_message, "text/html")
        email.send(fail_silently=False)

        logger.info(f"✅ Email sent successfully to {user.email}: {subject}")
        return True
    except TRANSIENT_EMAIL_ERRORS as e:
        # warning, а не error — сбой временный.
        logger.warning(
            f"⚠️ Временный сбой при отправке письма {user.email} (сеть/SMTP, "
            f"похоже на обрыв соединения, а не ошибку в коде): {type(e).__name__}: {e}"
        )
        if raise_on_transient:
            raise TransientEmailError(str(e)) from e
        return False
    except Exception as e:
        logger.error(f"❌ Failed to send email to {user.email}: {type(e).__name__}: {e}")
        return False


@shared_task(bind=True, max_retries=3, countdown=10)
def send_badge_earned_notification(self, user_id: str, badge_type: str, badge_name: str):
    """Мгновенное письмо о достижении (когда у пользователя выключен дайджест)."""
    try:
        from users.models import User
        user = User.objects.get(id=user_id)
        logger.info(f"📤 Processing badge email for {user.username}: {badge_name}")
        _send_email_to_user(
            user, f'Новое достижение: {badge_name}', 'emails/badge_earned.html',
            {'badge_name': badge_name}, notification_type='new_badge', raise_on_transient=True,
        )
        return True
    except User.DoesNotExist:
        logger.error(f"❌ User {user_id} not found for badge notification")
        return False
    except Exception as e:
        logger.error(f"❌ Error in send_badge_earned_notification: {e}", exc_info=True)
        raise self.retry(exc=e, countdown=60 * (2 ** self.request.retries))


@shared_task(bind=True, max_retries=3, countdown=10)
def send_level_up_notification(self, user_id: str, new_level: int, total_xp: int):
    """Мгновенное письмо о новом уровне."""
    try:
        from users.models import User
        user = User.objects.get(id=user_id)
        logger.info(f"📤 Processing level up email for {user.username}: Level {new_level}")
        _send_email_to_user(
            user, f'Вы достигли уровня {new_level}!', 'emails/level_up.html',
            {'new_level': new_level, 'total_xp': total_xp}, notification_type='level_up',
            raise_on_transient=True,
        )
        return True
    except User.DoesNotExist:
        return False
    except Exception as e:
        logger.error(f"❌ Error in send_level_up_notification: {e}", exc_info=True)
        raise self.retry(exc=e, countdown=60 * (2 ** self.request.retries))


@shared_task(bind=True, max_retries=3, countdown=5)
def send_email_verification(self, user_id: str, token: str):
    """Письмо верификации (force=True)."""
    try:
        from users.models import User
        user = User.objects.get(id=user_id)
        site_url = getattr(settings, 'SITE_URL', 'https://dopx.kz')
        verify_url = f"{site_url}/users/verify-email/{token}/"

        _send_email_to_user(
            user, 'Подтвердите email на DOPX', 'emails/verify_email.html', {'verify_url': verify_url},
            force=True, raise_on_transient=True,
        )
        return True
    except Exception as e:
        logger.error(f"❌ Error in send_email_verification: {e}", exc_info=True)
        raise self.retry(exc=e, countdown=60 * (2 ** self.request.retries))


@shared_task(bind=True, max_retries=3, rate_limit='60/m')
def _send_match_email_chunk(
    self,
    user_ids: list[str],
    match_id: str,
    subject: str,
    template_name: str,
    notification_type: str,
) -> int:
    """Письмо одной пачке пользователей. rate_limit ограничивает число пачек в минуту.
    При сетевом сбое пачка ретраится целиком (возможен безобидный дубль).
    """
    from matches.models import Match
    from users.models import User

    match = Match.objects.select_related('home_team', 'away_team').filter(id=match_id).first()
    if not match:
        logger.error(f"_send_match_email_chunk: match {match_id} not found")
        return 0

    sent = 0
    users = User.objects.filter(id__in=user_ids, is_verified=True, email__isnull=False)
    try:
        for user in users:
            if _send_email_to_user(
                user, subject, template_name, {'match': match},
                notification_type=notification_type, raise_on_transient=True,
            ):
                sent += 1
    except TransientEmailError as e:
        logger.warning(
            f"_send_match_email_chunk: сетевой сбой, ретраим пачку целиком "
            f"({sent} уже отправлено в этой попытке до обрыва): {e}"
        )
        raise self.retry(exc=e, countdown=60 * (2 ** self.request.retries))
    return sent


def _chunked(items: list, size: int) -> list[list]:
    return [items[i:i + size] for i in range(0, len(items), size)]


@shared_task(bind=True, max_retries=3, rate_limit='60/m')
def _send_system_announcement_chunk(self, user_ids: list[str], subject: str, title: str, body: str) -> int:
    """Пачка писем для системного объявления из дашборда. In-app уведомления
    создаёт сама вьюха, здесь только email (с учётом email_system).
    """
    from users.models import User

    sent = 0
    users = User.objects.filter(id__in=user_ids, is_verified=True, email__isnull=False)
    try:
        for user in users:
            if _send_email_to_user(
                user, subject, 'emails/system_announcement.html', {'title': title, 'body': body},
                notification_type='system', raise_on_transient=True,
            ):
                sent += 1
    except TransientEmailError as e:
        logger.warning(
            f"_send_system_announcement_chunk: сетевой сбой, ретраим пачку целиком "
            f"({sent} уже отправлено в этой попытке до обрыва): {e}"
        )
        raise self.retry(exc=e, countdown=60 * (2 ** self.request.retries))
    return sent


@shared_task(bind=True, max_retries=3)
def notify_voting_closing_soon(self):
    """Напоминание о скором закрытии голосования (каждые 30 минут).
    Дедуп: если Notification voting_closing для матча уже есть — матч пропускаем.
    """
    from django.urls import reverse

    from matches.models import Match

    now = timezone.now()
    closing_threshold = now + timedelta(hours=1)

    matches = list(Match.objects.filter(
        status='finished',
        voting_open_until__gte=now,
        voting_open_until__lte=closing_threshold
    ).select_related('home_team', 'away_team'))

    if not matches:
        logger.info(f"✅ No matches closing voting in the next hour (now={now}, threshold={closing_threshold})")
        return {'status': 'ok', 'matches_found': 0}

    from notifications.models import Notification
    from users.models import User

    already_notified_match_ids = set(
        Notification.objects.filter(
            notification_type='voting_closing',
            related_match_id__in=[m.id for m in matches],
        ).values_list('related_match_id', flat=True).distinct()
    )

    from core.models import get_setting

    user_ids = [
        str(uid) for uid in User.objects.filter(is_verified=True, email__isnull=False)
        .values_list('id', flat=True)
    ]
    chunks = _chunked(user_ids, get_setting("bulk_email_chunk_size", BULK_EMAIL_CHUNK_SIZE))

    queued = 0
    skipped = 0
    for match in matches:
        if match.id in already_notified_match_ids:
            skipped += 1
            continue

        subject = f'Голосование за матч {match.home_team.name} vs {match.away_team.name} скоро закроется'
        for chunk in chunks:
            _send_match_email_chunk.delay(chunk, str(match.id), subject, 'emails/voting_closing.html', 'voting_closing')
            queued += 1

        # Notification — заодно маркер дедупа для следующих прогонов.
        action_url = reverse('matches:detail', args=[match.id])
        Notification.objects.bulk_create([
            Notification(
                user_id=uid,
                notification_type='voting_closing',
                title=subject,
                message='Голосование за этот матч закрывается в течение часа — успейте оценить, пока не поздно.',
                action_url=action_url,
                related_match=match,
            )
            for uid in user_ids
        ])

    matches_processed = len(matches) - skipped
    logger.info(
        f"✅ Queued {queued} email chunk(s) across {matches_processed} closing-soon match(es), "
        f"{skipped} skipped as already notified earlier."
    )
    return {
        'status': 'ok',
        'matches_processed': matches_processed,
        'chunks_queued': queued,
        'skipped_already_notified': skipped,
    }


@shared_task
def send_notification_digest():
    """Раз в час собирает неотправленные Notification (new_badge/level_up/system)
    пользователей с email_digest_mode в одно письмо. С Redis-локом.
    """
    lock_key = "notifications:lock:send_notification_digest"
    if not cache.add(lock_key, "1", timeout=NOTIFY_TASK_LOCK_TIMEOUT):
        logger.info("send_notification_digest: уже выполняется другим воркером — пропускаем")
        return {'users_notified': 0, 'notifications_sent': 0, 'skipped_locked': True}

    try:
        from notifications.models import Notification

        pending = list(
            Notification.objects.filter(
                notification_type__in=DIGESTIBLE_NOTIFICATION_TYPES,
                email_sent_at__isnull=True,
            ).select_related('user').order_by('user_id', 'created_at')
        )

        if not pending:
            return {'users_notified': 0, 'notifications_sent': 0}

        by_user: dict[str, list] = {}
        for note in pending:
            by_user.setdefault(str(note.user_id), []).append(note)

        users_notified = 0
        notifications_sent = 0

        for user_id, notes in by_user.items():
            user = notes[0].user
            if not user.email or not user.is_verified:
                continue
            if not user.get_notification_setting('email_digest_mode', True):
                # Пользователь без дайджеста — ему письма уже ушли сразу.
                continue

            sent = _send_email_to_user(
                user,
                f'Ваши обновления на DOPX ({len(notes)})',
                'emails/notification_digest.html',
                {'notifications': notes, 'count': len(notes)},
                notification_type='system',
            )
            if sent:
                Notification.objects.filter(id__in=[n.id for n in notes]).update(email_sent_at=timezone.now())
                users_notified += 1
                notifications_sent += len(notes)

        logger.info(f"✅ Digest sent to {users_notified} user(s), {notifications_sent} notification(s) total.")
        return {'users_notified': users_notified, 'notifications_sent': notifications_sent}
    finally:
        cache.delete(lock_key)


@shared_task
def cleanup_old_notifications():
    """Удаляет прочитанные уведомления старше NOTIFICATION_RETENTION_DAYS."""
    from core.models import get_setting
    from notifications.models import Notification

    retention_days = get_setting("notification_retention_days", NOTIFICATION_RETENTION_DAYS)
    cutoff = timezone.now() - timedelta(days=retention_days)
    deleted_count, _ = Notification.objects.filter(is_read=True, created_at__lt=cutoff).delete()
    logger.info(f"🧹 Deleted {deleted_count} old read notification(s) older than {retention_days} days.")
    return {'deleted': deleted_count}


@shared_task(bind=True, max_retries=3, countdown=5)
def notify_followers_match_activity(self, match_id: str):
    """Приглашение оценить только что завершённый матч: in-app + push + email.

    Вызывается из импорта при переходе матча в finished. Аудитория — подписчики
    команд/игроков матча и все, кто ставил прогноз на этот матч.
    Email учитывает email_match_finished.
    """
    from django.db.models import Q
    from django.urls import reverse

    from lineups.models import MatchLineupPlayer
    from matches.models import Match
    from notifications.models import Notification
    from predictions.models import MatchPrediction
    from users.models import Follow

    match = Match.objects.select_related('home_team', 'away_team').filter(id=match_id).first()
    if not match:
        logger.error(f"notify_followers_match_activity: match {match_id} not found")
        return {'notified': 0}

    player_ids = list(
        MatchLineupPlayer.objects.filter(lineup__match=match)
        .values_list('player_id', flat=True)
        .distinct()
    )

    follower_user_ids = set(
        Follow.objects.filter(
            Q(team_id__in=[match.home_team_id, match.away_team_id]) | Q(player_id__in=player_ids)
        ).values_list('user_id', flat=True)
    )
    predictor_user_ids = set(
        MatchPrediction.objects.filter(match=match).values_list('user_id', flat=True)
    )
    audience_user_ids = follower_user_ids | predictor_user_ids

    if not audience_user_ids:
        return {'notified': 0}

    title = f"{match.home_team.name} {match.get_score_display()} {match.away_team.name}"
    message = (
        "Матч завершён — вы за ним следили или ставили прогноз. "
        "Голосование открыто 48 часов — поделитесь своим мнением."
    )
    action_url = reverse('matches:detail', args=[match.id])

    Notification.objects.bulk_create([
        Notification(
            user_id=uid,
            notification_type='voting_open',
            title=title,
            message=message,
            action_url=action_url,
            related_match=match,
        )
        for uid in audience_user_ids
    ])

    # Push best-effort: ошибки не должны ломать рассылку остальным.
    try:
        from notifications.services import send_push_to_user
        from users.models import User

        for user in User.objects.filter(id__in=audience_user_ids):
            send_push_to_user(user, title=title, body=message, url=action_url)
    except Exception as exc:
        logger.warning(f"notify_followers_match_activity: push fan-out skipped: {exc}")

    # Аудитория небольшая — email шлём напрямую, без пачек.
    from users.models import User as _UserModel

    emailed = 0
    for user in _UserModel.objects.filter(id__in=audience_user_ids, is_verified=True, email__isnull=False):
        if _send_email_to_user(
            user,
            f'{title} — голосование открыто',
            'emails/voting_open.html',
            {'match': match, 'title': title},
            notification_type='voting_open',
        ):
            emailed += 1

    logger.info(f"✅ Notified {len(audience_user_ids)} user(s) about match {match.id} ({emailed} email(s) sent)")
    return {'notified': len(audience_user_ids), 'emailed': emailed}


def _match_notification_audience(match) -> set[str]:
    """Аудитория для пушей вокруг матча: подписчики команд/игроков + сделавшие прогноз."""
    from django.db.models import Q

    from lineups.models import MatchLineupPlayer
    from predictions.models import MatchPrediction
    from users.models import Follow

    player_ids = list(
        MatchLineupPlayer.objects.filter(lineup__match=match)
        .values_list('player_id', flat=True)
        .distinct()
    )
    follower_user_ids = set(
        Follow.objects.filter(
            Q(team_id__in=[match.home_team_id, match.away_team_id]) | Q(player_id__in=player_ids)
        ).values_list('user_id', flat=True)
    )
    predictor_user_ids = set(
        MatchPrediction.objects.filter(match=match).values_list('user_id', flat=True)
    )
    return follower_user_ids | predictor_user_ids


@shared_task(bind=True, max_retries=3, countdown=5)
def notify_followers_match_started(self, match_id: str):
    """Push + in-app при старте матча (переход scheduled -> live). Без email."""
    from django.urls import reverse

    from matches.models import Match
    from notifications.models import Notification

    match = Match.objects.select_related('home_team', 'away_team').filter(id=match_id).first()
    if not match:
        logger.error(f"notify_followers_match_started: match {match_id} not found")
        return {'notified': 0}

    audience_user_ids = _match_notification_audience(match)
    if not audience_user_ids:
        return {'notified': 0}

    title = f"⚽️ Матч начался: {match.home_team.name} — {match.away_team.name}"
    message = "Стартовый свисток прозвучал — следите за матчем в реальном времени."
    action_url = reverse('matches:detail', args=[match.id])

    Notification.objects.bulk_create([
        Notification(
            user_id=uid,
            notification_type='match_started',
            title=title,
            message=message,
            action_url=action_url,
            related_match=match,
        )
        for uid in audience_user_ids
    ])

    try:
        from notifications.services import send_push_to_user
        from users.models import User

        for user in User.objects.filter(id__in=audience_user_ids):
            send_push_to_user(user, title=title, body=message, url=action_url)
    except Exception as exc:
        logger.warning(f"notify_followers_match_started: push fan-out skipped: {exc}")

    logger.info(f"✅ Notified {len(audience_user_ids)} follower(s) about match {match.id} kickoff")
    return {'notified': len(audience_user_ids)}


@shared_task(bind=True, max_retries=3, countdown=5)
def notify_followers_lineups_available(self, match_id: str):
    """Push + in-app, когда появились составы (только пока матч не завершён)."""
    from django.urls import reverse

    from matches.models import Match
    from notifications.models import Notification

    match = Match.objects.select_related('home_team', 'away_team').filter(id=match_id).first()
    if not match:
        logger.error(f"notify_followers_lineups_available: match {match_id} not found")
        return {'notified': 0}

    audience_user_ids = _match_notification_audience(match)
    if not audience_user_ids:
        return {'notified': 0}

    title = f"📋 Составы объявлены: {match.home_team.name} — {match.away_team.name}"
    message = "Стартовые составы уже на сайте — посмотрите, кто выйдет на поле."
    action_url = reverse('matches:detail', args=[match.id])

    Notification.objects.bulk_create([
        Notification(
            user_id=uid,
            notification_type='lineups_available',
            title=title,
            message=message,
            action_url=action_url,
            related_match=match,
        )
        for uid in audience_user_ids
    ])

    try:
        from notifications.services import send_push_to_user
        from users.models import User

        for user in User.objects.filter(id__in=audience_user_ids):
            send_push_to_user(user, title=title, body=message, url=action_url)
    except Exception as exc:
        logger.warning(f"notify_followers_lineups_available: push fan-out skipped: {exc}")

    logger.info(f"✅ Notified {len(audience_user_ids)} follower(s) about lineups for match {match.id}")
    return {'notified': len(audience_user_ids)}


# События матча, по которым шлём live-push.
PUSH_WORTHY_EVENT_TYPES = frozenset({'goal', 'own_goal', 'penalty', 'disallowed_goal', 'red_card'})


@shared_task(bind=True, max_retries=2)
def notify_followers_match_event(self, match_id: str, event_id: str):
    """Live-push по событию матча (гол, автогол, пенальти, отменённый гол, красная)
    подписчикам команд или игрока. Только push + in-app, без email.
    Ставится из импорта для новых событий.
    """
    from django.db.models import Q
    from django.urls import reverse

    from events.models import MatchEvent
    from matches.models import Match
    from notifications.models import Notification
    from users.models import Follow

    event = MatchEvent.objects.select_related('match__home_team', 'match__away_team', 'player').filter(
        id=event_id
    ).first()
    if not event:
        logger.error(f"notify_followers_match_event: event {event_id} not found")
        return {'notified': 0}

    match = event.match
    if str(match.id) != str(match_id):
        # Защита от рассинхрона match_id.
        logger.error(f"notify_followers_match_event: event {event_id} belongs to match {match.id}, not {match_id}")
        return {'notified': 0}

    follower_user_ids = set(
        Follow.objects.filter(
            Q(team_id__in=[match.home_team_id, match.away_team_id]) | Q(player_id=event.player_id)
        ).values_list('user_id', flat=True)
    )
    if not follower_user_ids:
        return {'notified': 0}

    score = match.get_score_display()
    home = match.home_team.name
    away = match.away_team.name
    # Если игрок не найден локально — имя берём из extra_data события.
    player_name = event.player_display_name

    if event.event_type == 'goal':
        title = f"⚽ Гол! {home} {score} {away}"
        message = f"{player_name} забивает на {event.display_minute}-й минуте." if player_name else f"Гол на {event.display_minute}-й минуте."
    elif event.event_type == 'own_goal':
        title = f"⚽ Автогол! {home} {score} {away}"
        message = f"{player_name} — автогол на {event.display_minute}-й минуте." if player_name else f"Автогол на {event.display_minute}-й минуте."
    elif event.event_type == 'penalty':
        title = f"🎯 Пенальти! {home} {score} {away}"
        message = f"{player_name} с пенальти на {event.display_minute}-й минуте." if player_name else f"Пенальти на {event.display_minute}-й минуте."
    elif event.event_type == 'disallowed_goal':
        title = f"❌ Гол отменён (VAR) — {home} {score} {away}"
        message = f"Гол на {event.display_minute}-й минуте отменён после проверки VAR."
    elif event.event_type == 'red_card':
        title = f"🟥 Красная карточка — {home} {score} {away}"
        message = f"{player_name} получает красную карточку на {event.display_minute}-й минуте." if player_name else f"Красная карточка на {event.display_minute}-й минуте."
    else:
        # Неподходящий тип — тихо выходим.
        logger.warning(f"notify_followers_match_event: неожиданный event_type={event.event_type!r} для события {event.id}, пропуск")
        return {'notified': 0}

    action_url = reverse('matches:detail', args=[match.id])

    Notification.objects.bulk_create([
        Notification(
            user_id=uid,
            notification_type='match_event',
            title=title,
            message=message,
            action_url=action_url,
            related_match=match,
        )
        for uid in follower_user_ids
    ])

    # Push best-effort.
    try:
        from notifications.services import send_push_to_user
        from users.models import User

        for user in User.objects.filter(id__in=follower_user_ids):
            send_push_to_user(user, title=title, body=message, url=action_url)
    except Exception as exc:
        logger.warning(f"notify_followers_match_event: push fan-out skipped: {exc}")

    logger.info(f"✅ Notified {len(follower_user_ids)} follower(s) about event {event.id} ({event.event_type}) in match {match.id}")
    return {'notified': len(follower_user_ids)}


# ============================================================
# Петли удержания: приглашение к прогнозу, результат прогноза, недельная сводка.
# Серии начисляются синхронно, майлстоуны — через бейджи.
# ============================================================

@shared_task(bind=True, max_retries=3)
def notify_prediction_closing_soon(self):
    """Приглашение сделать прогноз примерно за час до матча — тем, кто ещё не сделал.
    Push + in-app + email. Без дедупа: повтор в узком окне допустим.
    """
    from django.urls import reverse

    from matches.models import Match
    from notifications.models import Notification
    from predictions.models import MatchPrediction

    now = timezone.now()
    closing_threshold = now + timedelta(hours=1)

    matches = Match.objects.filter(
        status='scheduled',
        start_time__gte=now,
        start_time__lte=closing_threshold,
    ).select_related('home_team', 'away_team')

    if not matches.exists():
        logger.info(f"✅ No matches kicking off in the next hour (now={now}).")
        return {'status': 'ok', 'matches_found': 0}

    from core.models import get_setting
    from users.models import User

    chunk_size = get_setting("bulk_email_chunk_size", BULK_EMAIL_CHUNK_SIZE)
    queued = 0
    notified_inapp = 0
    for match in matches:
        already_predicted = MatchPrediction.objects.filter(match=match).values('user_id')
        user_ids = [
            str(uid) for uid in User.objects.filter(is_verified=True, email__isnull=False)
            .exclude(id__in=already_predicted)
            .values_list('id', flat=True)
        ]
        if not user_ids:
            continue

        subject = f'{match.home_team.name} — {match.away_team.name}: как думаете, кто победит?'
        for chunk in _chunked(user_ids, chunk_size):
            _send_match_email_chunk.delay(chunk, str(match.id), subject, 'emails/prediction_closing.html', 'prediction_closing')
            queued += 1

        title = f'{match.home_team.name} vs {match.away_team.name} — кто победит?'
        message = 'Матч начинается через час. Успейте поставить прогноз на исход, пока приём открыт.'
        action_url = reverse('matches:detail', args=[match.id])

        Notification.objects.bulk_create([
            Notification(
                user_id=uid, notification_type='prediction_closing',
                title=title, message=message, action_url=action_url, related_match=match,
            )
            for uid in user_ids
        ])
        notified_inapp += len(user_ids)

        # Push best-effort.
        try:
            from notifications.services import send_push_to_user

            for user in User.objects.filter(id__in=user_ids):
                send_push_to_user(user, title=title, body=message, url=action_url)
        except Exception as exc:
            logger.warning(f"notify_prediction_closing_soon: push fan-out skipped for match {match.id}: {exc}")

    logger.info(
        f"✅ Queued {queued} email chunk(s), {notified_inapp} in-app notification(s) "
        f"across {matches.count()} match(es) starting soon."
    )
    return {
        'status': 'ok', 'matches_processed': matches.count(),
        'chunks_queued': queued, 'inapp_notified': notified_inapp,
    }


@shared_task(bind=True, max_retries=3)
def notify_prediction_results(self):
    """Результат прогноза каждому, кто его ставил: in-app + email.

    Дедуп — по существующей Notification prediction_result для пары (матч, user),
    плюс Redis-лок от параллельных прогонов. Обычное окно — 6 часов, но матчи
    старше досылаются (до prediction_results_catchup_days), если воркер простаивал.
    """
    lock_key = "notifications:lock:notify_prediction_results"
    if not cache.add(lock_key, "1", timeout=NOTIFY_TASK_LOCK_TIMEOUT):
        logger.info("notify_prediction_results: уже выполняется другим воркером — пропускаем")
        return {'notified': 0, 'skipped_locked': True}

    try:
        from django.urls import reverse

        from core.models import get_setting
        from matches.models import Match
        from notifications.models import Notification
        from predictions.models import MatchPrediction
        from predictions.services import prediction_counts
        from users.tasks import check_and_award_badges_task

        now = timezone.now()
        lookback = now - timedelta(hours=6)
        # Потолок досылки при простое воркера; дедуп — по Notification.
        catchup_cutoff = now - timedelta(days=get_setting("prediction_results_catchup_days", 30))

        # Порядок по end_time важен для серии прогнозов.
        matches = Match.objects.filter(
            status='finished', end_time__isnull=False, end_time__gte=catchup_cutoff, end_time__lte=now,
        ).select_related('home_team', 'away_team').order_by('end_time')

        stale_matches = [m for m in matches if m.end_time < lookback]
        if stale_matches:
            logger.warning(
                "notify_prediction_results: catch-up сработал для %d матч(ей) старше "
                "обычного 6-часового окна (id: %s) — вероятно, воркер/beat простаивал "
                "дольше обычного; уведомления досылаются задним числом.",
                len(stale_matches), [m.id for m in stale_matches],
            )

        result_labels = {'1': 'Победа хозяев', 'X': 'Ничья', '2': 'Победа гостей'}
        notified = 0

        for match in matches:
            # Дедуп по успешной отправке (email_sent_at). См. docs/adr/0020-prediction-result-email-dedup.md.
            already_emailed = Notification.objects.filter(
                notification_type='prediction_result', related_match=match, email_sent_at__isnull=False,
            ).values('user_id')
            predictions = list(
                MatchPrediction.objects.filter(match=match)
                .exclude(user_id__in=already_emailed)
                .select_related('user')
            )
            if not predictions:
                continue

            counts = prediction_counts(match)
            action_url = reverse('matches:detail', args=[match.id])
            your_choice_labels = {
                '1': f'П1 ({match.home_team.name})',
                'X': 'Х (ничья)',
                '2': f'П2 ({match.away_team.name})',
            }

            existing_by_user = {
                n.user_id: n
                for n in Notification.objects.filter(
                    notification_type='prediction_result', related_match=match,
                    user_id__in=[pred.user_id for pred in predictions],
                )
            }

            notif_by_user = {}
            notifications_to_create = []
            for pred in predictions:
                is_correct = pred.is_correct
                title = "✅ Ваш прогноз сбылся!" if is_correct else "Прогноз не сбылся"
                message = (
                    f"{match.home_team.name} {match.get_score_display()} {match.away_team.name} — "
                    f"{result_labels.get(match.final_result, '?')}. "
                    f"Ваш прогноз: {your_choice_labels.get(pred.choice, pred.choice)}."
                )
                existing = existing_by_user.get(pred.user_id)
                if existing:
                    # Запись от прошлого прогона без отправленного письма — только повторяем письмо,
                    # серию второй раз не трогаем.
                    notif_by_user[pred.user_id] = existing
                    continue
                notif = Notification(
                    user=pred.user,
                    notification_type='prediction_result',
                    title=title,
                    message=message,
                    action_url=action_url,
                    related_match=match,
                    # email_sent_at ставим только после успешной отправки.
                )
                notifications_to_create.append(notif)
                notif_by_user[pred.user_id] = notif

                # Серия прогнозов обновляется один раз — при первом создании Notification.
                pred.user.update_prediction_stats(is_correct)
                check_and_award_badges_task.delay(user_id=str(pred.user_id), match_id=str(match.id))

            Notification.objects.bulk_create(notifications_to_create)

            for pred in predictions:
                sent_ok = _send_email_to_user(
                    pred.user,
                    f'{"Прогноз сбылся" if pred.is_correct else "Итог матча"}: {match.home_team.name} vs {match.away_team.name}',
                    'emails/prediction_result.html',
                    {
                        'match': match,
                        'counts': counts,
                        'is_correct': pred.is_correct,
                        'your_choice_label': your_choice_labels.get(pred.choice, pred.choice),
                    },
                    notification_type='prediction_result',
                )
                if sent_ok:
                    notif = notif_by_user[pred.user_id]
                    notif.email_sent_at = timezone.now()
                    notif.save(update_fields=['email_sent_at', 'updated_at'])

            notified += len(predictions)

        logger.info(f"✅ notify_prediction_results: notified {notified} predictor(s) across {matches.count()} match(es).")
        return {'notified': notified}
    finally:
        cache.delete(lock_key)


@shared_task(bind=True, max_retries=3)
def send_weekly_summary(self):
    """Персональная сводка недели: оценки, прогнозы, точность, матч недели.
    Вне дайджеста, только для включивших email_weekly_summary.
    """
    from django.db.models import Count, Q

    from evaluations.models import EvaluationSession
    from matches.models import Match
    from predictions.models import MatchPrediction
    from users.models import User

    now = timezone.now()
    week_ago = now - timedelta(days=7)

    # Матч недели считаем один раз на всю рассылку.
    top_match = (
        Match.objects.filter(
            status='finished', end_time__gte=week_ago, end_time__lte=now,
            aggregate__isnull=False,
        )
        .select_related('home_team', 'away_team', 'aggregate')
        .order_by('-aggregate__avg_entertainment')
        .first()
    )

    users = User.objects.filter(is_verified=True, email__isnull=False)
    sent = 0

    for user in users:
        if not user.get_notification_setting('email_weekly_summary', True):
            continue

        evaluations_count = EvaluationSession.objects.filter(
            user=user, status='completed', completed_at__gte=week_ago, completed_at__lt=now,
        ).count()

        week_predictions = MatchPrediction.objects.filter(
            user=user, created_at__gte=week_ago, created_at__lt=now,
        ).select_related('match')
        predictions_count = week_predictions.count()

        # Точность — только по матчам с известным исходом.
        decided = [p for p in week_predictions if p.match.final_result is not None]
        accuracy_pct = None
        if decided:
            correct = sum(1 for p in decided if p.choice == p.match.final_result)
            accuracy_pct = round(correct * 100 / len(decided))

        if evaluations_count == 0 and predictions_count == 0:
            # Нет активности за неделю — письмо не шлём.
            continue

        if _send_email_to_user(
            user,
            'Ваша неделя на DOPX',
            'emails/weekly_summary.html',
            {
                'evaluations_count': evaluations_count,
                'predictions_count': predictions_count,
                'accuracy_pct': accuracy_pct,
                'top_match': top_match,
            },
            notification_type='weekly_digest',
        ):
            sent += 1

    logger.info(f"✅ send_weekly_summary: sent to {sent} user(s).")
    return {'sent': sent}


@shared_task(bind=True, max_retries=3)
def send_staff_antifraud_digest(self):
    """Недельная сводка антифрода для staff: новые сигналы за 7 дней по источникам,
    топ-3 по score, открытые диспуты. Пустую сводку не шлём. force=True.
    """
    from django.contrib.contenttypes.models import ContentType

    from users.models import SuspiciousActivityFlag, User

    since = timezone.now() - timedelta(days=7)

    new_flags = list(
        SuspiciousActivityFlag.objects.filter(created_at__gte=since)
        .select_related("user", "match", "content_type")
        .order_by("-score", "-created_at")
    )
    if not new_flags:
        logger.info("send_staff_antifraud_digest: за неделю новых флагов нет, письмо не отправляется.")
        return {'sent': 0}

    by_source: dict[str, int] = {}
    for flag in new_flags:
        by_source[flag.get_source_display()] = by_source.get(flag.get_source_display(), 0) + 1

    top_flags = new_flags[:3]

    from notifications.models import ContactSubmission

    open_disputes = ContactSubmission.objects.filter(
        category="dispute", status__in=["new", "in_progress"]
    ).count()

    recipients = User.objects.filter(is_staff=True, is_active=True).exclude(email="").exclude(email__isnull=True)
    sent = 0
    for staff_user in recipients:
        if _send_email_to_user(
            staff_user,
            f'Антифрод за неделю: {len(new_flags)} новых сигналов',
            'emails/staff_antifraud_digest.html',
            {
                'total_new': len(new_flags),
                'by_source': by_source,
                'top_flags': top_flags,
                'open_disputes': open_disputes,
            },
            force=True,
        ):
            sent += 1

    logger.info(f"✅ send_staff_antifraud_digest: sent to {sent} staff member(s), {len(new_flags)} new flag(s) this week.")
    return {'sent': sent, 'new_flags': len(new_flags)}