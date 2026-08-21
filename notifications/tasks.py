# notifications/tasks.py
"""
_send_email_to_user проверяет настройку через явный notification_type
(маппинг на ключ настроек словарём), не парсит тему письма строкой — иначе
письмо о закрытии голосования содержит слово "Голосование" и попадает не
в ту ветку. Массовые рассылки (send_voting_open_notification,
notify_voting_closing_soon) — fan-out: родительская задача ставит в очередь
пачки по BULK_EMAIL_CHUNK_SIZE через _send_match_email_chunk, каждая со
своим rate_limit — иначе риск упереться в CELERY_TASK_TIME_LIMIT и при
ретрае разослать всё заново. send_notification_digest — периодическая
задача, собирает не отправленные по email Notification для пользователей
с email_digest_mode=True в одно письмо вместо N отдельных.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils import timezone

logger = logging.getLogger(__name__)

# Сколько получателей в одной "пачке" при fan-out массовой рассылки —
# см. пункт 2 докстринга модуля.
BULK_EMAIL_CHUNK_SIZE = 50

# Сколько дней хранить уже прочитанные уведомления — см. пункт 4.
NOTIFICATION_RETENTION_DAYS = 90

# Единственное место маппинга "тип уведомления -> ключ настройки" —
# см. пункт 1 докстринга модуля. `None` — уведомление всегда критическое,
# отправляется только через force=True и сюда не попадает.
NOTIFICATION_TYPE_TO_SETTINGS_KEY: dict[str, str] = {
    "match_finished": "email_match_finished",
    "voting_open": "email_match_finished",
    "voting_closing": "email_voting_closing",
    "new_badge": "email_new_badge",
    "level_up": "email_level_up",
    "system": "email_system",
    # НОВОЕ (4 петли удержания, 2026-08-21):
    "prediction_closing": "email_prediction_closing",
    "prediction_result": "email_prediction_result",
    "weekly_digest": "email_weekly_summary",
}

# Уведомления этих типов собираются в дайджест (см. пункт 3), а не
# отправляются мгновенно, если у пользователя включён `email_digest_mode`.
DIGESTIBLE_NOTIFICATION_TYPES = ("new_badge", "level_up", "system")


def _send_email_to_user(
    user,
    subject: str,
    template_name: str,
    context: dict,
    notification_type: str | None = None,
    force: bool = False,
) -> bool:
    """
    Безопасная отправка email.

    :param notification_type: явный тип уведомления (см.
        `NOTIFICATION_TYPE_TO_SETTINGS_KEY`) — используется для проверки
        настроек пользователя ВМЕСТО парсинга текста темы письма (см. пункт
        1 докстринга модуля). Игнорируется, если `force=True`.
    :param force: игнорирует настройки пользователя (для верификации,
        сброса пароля и т.д.).
    """
    if not user or not user.email:
        logger.warning("⚠️ Cannot send email: user or email is missing")
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
    """
    Отправка МГНОВЕННОГО письма о достижении.

    Вызывается только если у пользователя ВЫКЛЮЧЕН `email_digest_mode` —
    иначе письмо соберётся в `send_notification_digest` (проверка теперь
    делается на стороне вызывающего кода — `users/tasks.py::
    check_and_award_badges_task`, — чтобы не ставить в очередь лишнюю
    задачу, которая всё равно ничего не отправит).
    """
    try:
        from users.models import User
        user = User.objects.get(id=user_id)
        logger.info(f"📤 Processing badge email for {user.username}: {badge_name}")
        _send_email_to_user(
            user, f'🎖️ Новое достижение: {badge_name}', 'emails/badge_earned.html',
            {'badge_name': badge_name}, notification_type='new_badge',
        )
        return True
    except User.DoesNotExist:
        logger.error(f"❌ User {user_id} not found for badge notification")
        return False
    except Exception as e:
        logger.error(f"❌ Error in send_badge_earned_notification: {e}", exc_info=True)
        raise self.retry(exc=e, countdown=60)


@shared_task(bind=True, max_retries=3, countdown=10)
def send_level_up_notification(self, user_id: str, new_level: int, total_xp: int):
    """Отправка МГНОВЕННОГО письма о повышении уровня (см. докстринг `send_badge_earned_notification`)."""
    try:
        from users.models import User
        user = User.objects.get(id=user_id)
        logger.info(f"📤 Processing level up email for {user.username}: Level {new_level}")
        _send_email_to_user(
            user, f'⬆️ Вы достигли уровня {new_level}!', 'emails/level_up.html',
            {'new_level': new_level, 'total_xp': total_xp}, notification_type='level_up',
        )
        return True
    except User.DoesNotExist:
        return False
    except Exception as e:
        logger.error(f"❌ Error in send_level_up_notification: {e}", exc_info=True)
        raise self.retry(exc=e, countdown=60)


@shared_task(bind=True, max_retries=3, countdown=5)
def send_email_verification(self, user_id: str, token: str):
    """Критическое письмо верификации (force=True — не подчиняется настройкам/дайджесту)."""
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


@shared_task(bind=True, max_retries=3, rate_limit='60/m')
def _send_match_email_chunk(
    self,
    user_ids: list[str],
    match_id: str,
    subject: str,
    template_name: str,
    notification_type: str,
) -> int:
    """
    Отправляет письмо одной пачке пользователей (см. пункт 2 докстринга
    модуля). `rate_limit='60/m'` — троттлинг на уровне Celery ограничивает,
    сколько ТАКИХ пачек может исполняться в минуту суммарно по всем
    воркерам, независимо от того, сколько пачек поставлено в очередь сразу.
    """
    from matches.models import Match
    from users.models import User

    match = Match.objects.select_related('home_team', 'away_team').filter(id=match_id).first()
    if not match:
        logger.error(f"_send_match_email_chunk: match {match_id} not found")
        return 0

    sent = 0
    users = User.objects.filter(id__in=user_ids, is_verified=True, email__isnull=False)
    for user in users:
        if _send_email_to_user(user, subject, template_name, {'match': match}, notification_type=notification_type):
            sent += 1
    return sent


def _chunked(items: list, size: int) -> list[list]:
    return [items[i:i + size] for i in range(0, len(items), size)]


@shared_task(bind=True, max_retries=3, countdown=5)
def send_voting_open_notification(self, match_id: str):
    """
    Оповещение о завершении матча и открытии голосования — теперь fan-out
    вместо синхронного цикла по всем пользователям (см. пункт 2 докстринга
    модуля).
    """
    try:
        from matches.models import Match
        from users.models import User

        match = Match.objects.select_related('home_team', 'away_team').filter(id=match_id).first()
        if not match:
            logger.error(f"send_voting_open_notification: match {match_id} not found")
            return {'queued_chunks': 0, 'total_users': 0}

        subject = f'🏁 Матч завершён: {match.home_team.name} vs {match.away_team.name}'
        user_ids = [
            str(uid) for uid in User.objects.filter(is_verified=True, email__isnull=False)
            .values_list('id', flat=True)
        ]

        chunks = _chunked(user_ids, BULK_EMAIL_CHUNK_SIZE)
        for chunk in chunks:
            _send_match_email_chunk.delay(chunk, match_id, subject, 'emails/voting_open.html', 'match_finished')

        logger.info(f"✅ Queued {len(chunks)} email chunk(s) ({len(user_ids)} users) for match {match_id}")
        return {'queued_chunks': len(chunks), 'total_users': len(user_ids)}
    except Exception as e:
        logger.error(f"❌ Error in send_voting_open_notification: {e}", exc_info=True)
        raise self.retry(exc=e, countdown=60)


@shared_task(bind=True, max_retries=3)
def notify_voting_closing_soon(self):
    """
    Напоминание о скором закрытии голосования — тоже fan-out на уровне
    пачек, плюс явный notification_type='voting_closing'.
    """
    from matches.models import Match

    now = timezone.now()
    closing_threshold = now + timedelta(hours=1)

    matches = Match.objects.filter(
        status='finished',
        voting_open_until__gte=now,
        voting_open_until__lte=closing_threshold
    ).select_related('home_team', 'away_team')

    if not matches.exists():
        logger.info(f"✅ No matches closing voting in the next hour (now={now}, threshold={closing_threshold})")
        return {'status': 'ok', 'matches_found': 0}

    from users.models import User

    user_ids = [
        str(uid) for uid in User.objects.filter(is_verified=True, email__isnull=False)
        .values_list('id', flat=True)
    ]
    chunks = _chunked(user_ids, BULK_EMAIL_CHUNK_SIZE)

    queued = 0
    for match in matches:
        subject = f'⏰ Голосование за матч {match.home_team.name} vs {match.away_team.name} скоро закроется!'
        for chunk in chunks:
            _send_match_email_chunk.delay(chunk, str(match.id), subject, 'emails/voting_closing.html', 'voting_closing')
            queued += 1

    logger.info(f"✅ Queued {queued} email chunk(s) across {matches.count()} closing-soon match(es).")
    return {'status': 'ok', 'matches_processed': matches.count(), 'chunks_queued': queued}


@shared_task
def send_notification_digest():
    """
    Периодическая задача: собирает Notification (email_sent_at__isnull=True)
    типов new_badge/level_up/system по пользователям с email_digest_mode=True
    и шлёт одно письмо-сводку вместо N мгновенных.
    """
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
            # Пользователь предпочитает мгновенные письма — дайджест их не трогает
            # (они уже были отправлены мгновенно и помечены email_sent_at при
            # создании — см. users/tasks.py, evaluations/views.py).
            continue

        sent = _send_email_to_user(
            user,
            f'📋 Ваши обновления на DOPX ({len(notes)})',
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


@shared_task
def cleanup_old_notifications():
    """
    НОВОЕ — реальная реализация вместо несуществующей задачи, на которую
    годами ссылался `CELERY_BEAT_SCHEDULE['voting-reminders']` (см. пункт 4
    докстринга модуля). Удаляет ПРОЧИТАННЫЕ уведомления старше
    `NOTIFICATION_RETENTION_DAYS` дней, чтобы таблица `Notification` не
    росла бесконечно. Непрочитанные не трогает — пользователь должен
    успеть их увидеть независимо от возраста.
    """
    from notifications.models import Notification

    cutoff = timezone.now() - timedelta(days=NOTIFICATION_RETENTION_DAYS)
    deleted_count, _ = Notification.objects.filter(is_read=True, created_at__lt=cutoff).delete()
    logger.info(f"🧹 Deleted {deleted_count} old read notification(s) older than {NOTIFICATION_RETENTION_DAYS} days.")
    return {'deleted': deleted_count}


@shared_task(bind=True, max_retries=3, countdown=5)
def notify_followers_match_activity(self, match_id: str):
    """
    Продуктовый аудит, раздел 5b ("Follow-граф"): адресное уведомление
    ТОЛЬКО тем, кто подписан на одну из играющих команд или на игрока в
    составе этого матча — в отличие от `send_voting_open_notification`
    (широковещательная рассылка ВСЕМ верифицированным пользователям, email).
    Ставится в очередь из `parsers/tasks.py::update_match_statuses` через
    `transaction.on_commit` в момент первого перехода матча в 'finished'.

    Намеренно только in-app `Notification`, БЕЗ email: follow-граф — новая,
    лёгкая фича без отдельного email-шаблона; если завести здесь ещё один
    email-канал, при будущем включении широковещательной `send_voting_
    open_notification` в `CELERY_BEAT_SCHEDULE` подписчики получили бы ДВА
    письма про один и тот же матч. In-app уведомление — самодостаточный
    MVP; email можно добавить отдельным шагом, когда решится, как
    дедуплицировать оба канала.
    """
    from django.db.models import Q
    from django.urls import reverse

    from lineups.models import MatchLineupPlayer
    from matches.models import Match
    from notifications.models import Notification
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

    if not follower_user_ids:
        return {'notified': 0}

    title = f"{match.home_team.name} {match.get_score_display()} {match.away_team.name}"
    message = (
        "Матч с командой или игроком, за которыми вы следите, завершён. "
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
        for uid in follower_user_ids
    ])

    # Push — лучшее из двух миров с in-app: следящий за игроком пользователь
    # часто НЕ сидит на сайте в момент финального свистка. Best-effort:
    # ошибка одного пользователя (устаревшая подписка и т.д.) не должна
    # прерывать рассылку остальным — см. try/except внутри send_push_to_user
    # самого по себе; здесь дополнительно оборачиваем весь цикл на случай
    # отсутствия pywebpush/VAPID-ключей в окружении.
    try:
        from notifications.services import send_push_to_user
        from users.models import User

        for user in User.objects.filter(id__in=follower_user_ids):
            send_push_to_user(user, title=title, body=message, url=action_url)
    except Exception as exc:
        logger.warning(f"notify_followers_match_activity: push fan-out skipped: {exc}")

    logger.info(f"✅ Notified {len(follower_user_ids)} follower(s) about match {match.id}")
    return {'notified': len(follower_user_ids)}


# ============================================================
# 4 петли удержания (retention loops), 2026-08-21 — задача пользователя:
# "нужна регулярная причина вернуться: прогнозы, персональная недельная
# сводка, «ваш прогноз/оценка против сообщества», серии". Ниже — loop 1
# (дедлайн прогноза) и loop 3 (прогноз vs результат). Loop 2 (недельная
# сводка) — тоже здесь, ниже. Loop 4 (серии) не требует отдельной задачи —
# начисление стрика синхронное (users/models.py::User.update_prediction_
# stats), а майлстоуны 7/30/100 идут через УЖЕ существующий пайплайн
# бейджей (check_and_award_badges_task → notification_type='new_badge'),
# см. users/badges.py и users/services.py.
# ============================================================

@shared_task(bind=True, max_retries=3)
def notify_prediction_closing_soon(self):
    """
    Loop 1 / приглашение к прогнозу в стиле Sofascore — ПЕРЕОСМЫСЛЕНО
    2026-08-21 по прямому запросу продукта (по мотивам того, как Sofascore
    шлёт пуш за час до матча: "команда А играет с командой Б — как вы
    думаете, кто победит?"). Раньше это письмо было чисто про срочность
    ("закрывается через час, успевайте") и только по email — теперь это
    ПРИГЛАШЕНИЕ поучаствовать, с тем же самым триггером по времени (~1 час
    до `Match.start_time`, только для тех, кто ещё не предсказал — см.
    `Match.is_prediction_open()`), но на два канала сразу: push (best-effort,
    тот же паттерн, что `notify_followers_match_activity`) + in-app
    `Notification`, ПЛЮС email с переписанным приглашающим текстом вместо
    urgency-формулировки (см. templates/emails/prediction_closing.html).

    С добавлением нижней границы окна прогноза (`Match.PREDICTION_WINDOW_DAYS`,
    matches/models.py) этот час перед стартом — по сути последний реалистичный
    момент напомнить: раньше — уже открыто и, скорее всего, увидено на
    странице матча, позже — уже поздно, прогноз закрылся вместе со стартовым
    свистком.

    Дедупликация НЕ нужна (в отличие от `notify_prediction_results`, где
    контент завязан на итоговый счёт и повтор был бы бессмысленным спамом):
    `crontab(minute='*/30')` может застать один и тот же матч в пределах
    часового окна дважды — оба раза увидит тех же ещё-не-предсказавших
    пользователей и пришлёт приглашение повторно. Это осознанно (и было так
    же у email-канала до этой правки) — короткое повторное напоминание в
    узком окне ближе к Sofascore-паттерну, чем риск ни разу не достучаться
    из-за пропущенного тика воркера.
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

    from users.models import User

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

        subject = f'⚽ {match.home_team.name} — {match.away_team.name}: как думаете, кто победит?'
        for chunk in _chunked(user_ids, BULK_EMAIL_CHUNK_SIZE):
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

        # Push — см. идентичный try/except-обёртку и обоснование в
        # notify_followers_match_activity выше: best-effort, сбой одного
        # пользователя/отсутствие VAPID-ключей не должен ронять всю задачу.
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
    """
    Loop 3: «ваш прогноз vs сообщество/результат» — персонализированное
    письмо+in-app уведомление КАЖДОМУ, кто ставил прогноз на матч, который
    недавно завершился.

    В отличие от `notify_followers_match_activity`/`send_voting_open_
    notification` (одна и та же тема/шаблон для всех адресатов, fan-out
    пачками), здесь контент у каждого получателя РАЗНЫЙ (свой выбор,
    совпал/не совпал) — фан-аут пачками неприменим без готового шаблона
    "письмо на пачку", поэтому цикл идёт по каждому предсказавшему
    напрямую внутри задачи (тот же стиль, что и `send_notification_digest`
    ниже — периодическая задача с прямым циклом рассылки, а не
    delegation на суб-задачи).

    Дедупликация БЕЗ отдельного булева флага на `MatchPrediction`: если
    для пары (match, user) уже существует `Notification(notification_type=
    'prediction_result', related_match=match, user=user)` — значит, письмо
    уже отправлено, повторный прогон `crontab(minute='*/30')` эту пару
    пропустит. `lookback` — 6 часов, не 1 — с запасом на случай простоя
    воркера/деплоя между прогонами; повторный прогон в пределах окна не
    дублирует уже обработанные пары благодаря дедупликации выше.
    """
    from django.urls import reverse

    from matches.models import Match
    from notifications.models import Notification
    from predictions.models import MatchPrediction
    from predictions.services import prediction_counts

    now = timezone.now()
    lookback = now - timedelta(hours=6)

    matches = Match.objects.filter(
        status='finished', end_time__isnull=False, end_time__gte=lookback, end_time__lte=now,
    ).select_related('home_team', 'away_team')

    result_labels = {'1': 'Победа хозяев', 'X': 'Ничья', '2': 'Победа гостей'}
    notified = 0

    for match in matches:
        already_notified = Notification.objects.filter(
            notification_type='prediction_result', related_match=match,
        ).values('user_id')
        predictions = list(
            MatchPrediction.objects.filter(match=match)
            .exclude(user_id__in=already_notified)
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

        notifications_to_create = []
        for pred in predictions:
            is_correct = pred.is_correct  # bool, т.к. match.final_result уже точно известен (status='finished')
            title = "✅ Ваш прогноз сбылся!" if is_correct else "Прогноз не сбылся"
            message = (
                f"{match.home_team.name} {match.get_score_display()} {match.away_team.name} — "
                f"{result_labels.get(match.final_result, '?')}. "
                f"Ваш прогноз: {your_choice_labels.get(pred.choice, pred.choice)}."
            )
            digest_mode = pred.user.get_notification_setting('email_digest_mode', True)
            notifications_to_create.append(Notification(
                user=pred.user,
                notification_type='prediction_result',
                title=title,
                message=message,
                action_url=action_url,
                related_match=match,
                # НЕ участвует в DIGESTIBLE_NOTIFICATION_TYPES (см. ниже) —
                # почтовая отправка идёт немедленно в этом же цикле, а не
                # через send_notification_digest, поэтому email_sent_at
                # проставляется сразу, а не по digest_mode пользователя.
                email_sent_at=timezone.now(),
            ))

        Notification.objects.bulk_create(notifications_to_create)

        for pred in predictions:
            _send_email_to_user(
                pred.user,
                f'{"✅" if pred.is_correct else "📊"} Итог матча {match.home_team.name} vs {match.away_team.name}',
                'emails/prediction_result.html',
                {
                    'match': match,
                    'counts': counts,
                    'is_correct': pred.is_correct,
                    'your_choice_label': your_choice_labels.get(pred.choice, pred.choice),
                },
                notification_type='prediction_result',
            )

        notified += len(predictions)

    logger.info(f"✅ notify_prediction_results: notified {notified} predictor(s) across {matches.count()} match(es).")
    return {'notified': notified}


@shared_task(bind=True, max_retries=3)
def send_weekly_summary(self):
    """
    Loop 2: персональная недельная сводка — сколько оценок/прогнозов сделал
    пользователь за последние 7 дней, точность прогнозов, "матч недели"
    (общий для всех, по средней вовлечённости из `aggregates.MatchAggregate`).

    Намеренно НЕ участвует в `send_notification_digest` (см. пункт 3
    докстринга модуля) — это САМА ПО СЕБЕ агрегированная сводка raz в
    неделю, оборачивать её ЕЩЁ раз в дайджест бессмысленно; письмо уходит
    сразу всем, кто включил `email_weekly_summary`, независимо от
    `email_digest_mode` (та же логика, что у мгновенных писем о
    завершении матча).

    Синхронный цикл по пользователям в одной задаче (не fan-out пачками)
    — контент персонализирован на каждого, как и `notify_prediction_
    results` выше; при росте базы пользователей на порядки это стоит
    переделать на chunked sub-tasks, для текущего масштаба KPL-аудитории
    один проход раз в неделю укладывается в `CELERY_TASK_TIME_LIMIT`.
    """
    from django.db.models import Count, Q

    from evaluations.models import EvaluationSession
    from matches.models import Match
    from predictions.models import MatchPrediction
    from users.models import User

    now = timezone.now()
    week_ago = now - timedelta(days=7)

    # "Матч недели" — один и тот же для всех писем этой рассылки, поэтому
    # считается ОДИН раз до цикла по пользователям, не на каждого.
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

        # Точность считается только по прогнозам с УЖЕ известным исходом
        # (match.final_result может быть None, если матч ещё не завершился
        # к моменту рассылки) — иначе делитель включал бы прогнозы, которые
        # физически не могли ни сбыться, ни провалиться.
        decided = [p for p in week_predictions if p.match.final_result is not None]
        accuracy_pct = None
        if decided:
            correct = sum(1 for p in decided if p.choice == p.match.final_result)
            accuracy_pct = round(correct * 100 / len(decided))

        if evaluations_count == 0 and predictions_count == 0:
            # Ничего не произошло за неделю — письмо "у вас 0 всего" не
            # несёт ценности и выглядит как упрёк, а не приглашение вернуться.
            continue

        if _send_email_to_user(
            user,
            '📊 Ваша неделя на DOPX',
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