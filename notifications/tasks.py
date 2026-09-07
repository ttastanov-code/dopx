# notifications/tasks.py
"""
_send_email_to_user проверяет настройку через явный notification_type
(маппинг на ключ настроек словарём), не парсит тему письма строкой — иначе
письмо о закрытии голосования содержит слово "Голосование" и попадает не
в ту ветку. Массовые рассылки (например, notify_voting_closing_soon,
notify_prediction_closing_soon) — fan-out: родительская задача ставит в очередь
пачки по BULK_EMAIL_CHUNK_SIZE через _send_match_email_chunk, каждая со
своим rate_limit — иначе риск упереться в CELERY_TASK_TIME_LIMIT и при
ретрае разослать всё заново. send_notification_digest — периодическая
задача, собирает не отправленные по email Notification для пользователей
с email_digest_mode=True в одно письмо вместо N отдельных.
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

# Временные (retry-able) сетевые сбои — не голый OSError (задел бы и
# нетранзиентные ошибки) и не SMTP-отказы вида Refused/DataError
# (постоянны, ретраить бессмысленно). См.
# docs/adr/0020-prediction-result-email-dedup.md.
TRANSIENT_EMAIL_ERRORS = (
    socket.gaierror,
    ConnectionError,
    TimeoutError,
    smtplib.SMTPServerDisconnected,
    smtplib.SMTPConnectError,
    smtplib.SMTPHeloError,
)


class TransientEmailError(Exception):
    """
    Сетевой/протокольный сбой при отправке письма (см. `TRANSIENT_EMAIL_ERRORS`).

    БАГ, КОТОРЫЙ ТУТ БЫЛ (Sentry, 2026-08-30 — see incident): `_send_email_
    to_user` ловил ЛЮБОЕ исключение внутри себя и просто возвращал False —
    ни одна вызывающая celery-задача не узнавала, что отправка сорвалась
    из-за временного сбоя (ноутбук с dev-сервером заснул/потерял сеть на
    ночь, пока Celery Beat продолжал тикать по расписанию), поэтому письмо
    терялось насовсем, а не переоправлялось. Теперь `_send_email_to_user`
    поднимает это исключение, если вызвана с `raise_on_transient=True`, —
    вызывающая задача ловит его и ретраит через `self.retry(...)` с
    backoff (см. `send_badge_earned_notification`, `_send_match_email_chunk`
    и т.д.). Для мест, где нельзя безопасно ретраить целиком (циклы по
    множеству пользователей в одной задаче — `notify_prediction_results` и
    похожие), исключение НЕ поднимается (используется дефолт
    `raise_on_transient=False`); там устойчивость к сбоям обеспечена иначе:
    "email_sent_at" проставляется только после реального успеха отправки, и
    непровалившиеся ранее адресаты естественным образом подхватываются
    следующим плановым прогоном той же периодической задачи.
    """

# Сколько получателей в одной "пачке" при fan-out массовой рассылки —
# см. пункт 2 докстринга модуля.
BULK_EMAIL_CHUNK_SIZE = 50

# Redis-lock TTL для периодических задач ниже (notify_prediction_results,
# send_notification_digest) — тот же cache.add()-паттерн (атомарный SETNX),
# что в season_squad/tasks.py::RECOMPUTE_LOCK_TIMEOUT и
# round_squad/tasks.py::ROUND_RECOMPUTE_LOCK_TIMEOUT: без лока два
# параллельных прогона (плановый тик Celery Beat + повторная доставка
# сообщения at-least-once) могли одновременно прочитать одну и ту же
# "необработанную" партию ДО того, как первый прогон успеет проставить
# признак обработки (создать Notification / выставить email_sent_at), и
# оба разослать письма. Значение — с запасом от реальной длительности
# одного прогона (обычно секунды-десятки секунд на текущих объёмах).
NOTIFY_TASK_LOCK_TIMEOUT = 600

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
    # НОВОЕ (2026-08-22): итоги «DOPX Лучшие тура», см. round_squad/tasks.py
    # ::send_round_results_notification.
    "round_results": "email_round_results",
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
    raise_on_transient: bool = False,
) -> bool:
    """
    Безопасная отправка email.

    :param notification_type: явный тип уведомления (см.
        `NOTIFICATION_TYPE_TO_SETTINGS_KEY`) — используется для проверки
        настроек пользователя ВМЕСТО парсинга текста темы письма (см. пункт
        1 докстринга модуля). Игнорируется, если `force=True`.
    :param force: игнорирует настройки пользователя (для верификации,
        сброса пароля и т.д.).
    :param raise_on_transient: True — при сетевом/протокольном сбое (см.
        `TRANSIENT_EMAIL_ERRORS`) поднять `TransientEmailError` вместо того,
        чтобы молча вернуть False. Включать там, где вызывающая
        celery-задача обрабатывает ОДНОГО получателя (или небольшую пачку) и
        может безопасно ретраить именно эту задачу — см. докстринг
        `TransientEmailError`.
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
            # Пустой text/plain-body ПОМИМО html-альтернативы — сигнал
            # спам-фильтров (письмо "только HTML", без единого читаемого
            # текста без рендеринга разметки, типично для спама/фишинга).
            # strip_tags() — быстрый, "достаточно хороший" plain-text из
            # уже отрендеренного HTML вместо поддержки отдельного .txt на
            # каждый из полутора десятков шаблонов писем в проекте.
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
        # warning, не error — это ожидаемо восстанавливаемый сбой (см.
        # TransientEmailError), а не баг в коде. logger.error() шёл бы в
        # Sentry с тем же уровнем тревожности, что и настоящая поломка.
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
    """Отправка МГНОВЕННОГО письма о повышении уровня (см. докстринг `send_badge_earned_notification`)."""
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
    """Критическое письмо верификации (force=True — не подчиняется настройкам/дайджесту)."""
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
    """
    Отправляет письмо одной пачке пользователей (см. пункт 2 докстринга
    модуля). `rate_limit='60/m'` — троттлинг на уровне Celery ограничивает,
    сколько ТАКИХ пачек может исполняться в минуту суммарно по всем
    воркерам, независимо от того, сколько пачек поставлено в очередь сразу.

    Ретраи при сетевых сбоях (см. `TransientEmailError`): это самый
    высокообъёмный путь доставки писем в проекте — через него идут
    voting_open/voting_closing/prediction_closing, то есть большинство
    реальных email-уведомлений. `raise_on_transient=True` — при обрыве
    DNS/TCP посреди цикла прерываем пачку и ретраим её целиком через
    self.retry с экспоненциальным backoff, вместо того чтобы тихо потерять
    письма всем, кто шёл в списке ПОСЛЕ сбойнувшего адреса (именно так
    терялись письма в инциденте 2026-08-30 — Sentry поймал gaierror/
    SMTPServerDisconnected, а задача просто продолжала цикл дальше, будто
    ничего не случилось). Компромисс: получатели из ЭТОЙ пачки (≤50 chelovek,
    см. BULK_EMAIL_CHUNK_SIZE), которым письмо уже ушло до обрыва, при
    ретрае получат его повторно — безобидный дубль, не критичная операция,
    дешевле, чем насовсем потерянные письма.
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
    """
    Пачка писем для staff-broadcast (см. dashboard/views.py::announcements —
    единственный вызывающий). Тот же fan-out-паттерн, что и
    `_send_match_email_chunk` выше, просто без привязки к конкретному
    матчу — контекст письма (`title`/`body`) один и тот же для всех пачек
    одной рассылки, передаётся напрямую, а не читается заново из БД.

    In-app-строки Notification создаются заранее, СИНХРОННО, одним
    bulk_create в самой вьюхе (не здесь) — они должны появиться у
    пользователя сразу после нажатия «Отправить», не ждать, пока Celery
    разберёт очередь чанков. Здесь — только email, с уважением к тумблеру
    `email_system` (см. `notification_type='system'` → `_send_email_to_user`
    → `NOTIFICATION_TYPE_TO_SETTINGS_KEY`).
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
    """
    Напоминание о скором закрытии голосования — тоже fan-out на уровне
    пачек, плюс явный notification_type='voting_closing'.

    Расписание (АКТУАЛЬНО): `crontab(minute='*/30')` — каждые 30 минут, см.
    `dopx/settings.py::CELERY_BEAT_SCHEDULE['voting-closing-reminders']`.
    Комментарий-заголовок секции рядом с этой записью в CELERY_BEAT_SCHEDULE
    ("каждые 6 часов") устарел и реальному crontab не соответствует — здесь
    эту цифру не повторяем, чтобы не тиражировать ту же ошибку дальше.

    БАГ, КОТОРЫЙ ТУТ БЫЛ: окно выборки — 1 час (`closing_threshold`), а сама
    задача гоняется каждые 30 минут → без дедупликации один и тот же
    закрывающийся матч почти всегда попадал в выборку ДВАЖДЫ подряд (на двух
    соседних тиках) и рассылка уходила всем пользователям дважды. Дедуп —
    тот же принцип, что в `notify_prediction_results` выше: перед постановкой
    email-чанков в очередь для конкретного матча проверяем, нет ли уже
    `Notification(notification_type='voting_closing', related_match=match)`
    — если есть, матч уже обработан прошлым тиком, пропускаем. Заодно теперь
    создаём эти Notification (по одной на пользователя) — раньше это
    напоминание существовало ТОЛЬКО как email, без in-app записи, хотя
    `notification_type='voting_closing'` в `NOTIFICATION_TYPES`
    (notifications/models.py) был заведён именно под него.
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

    user_ids = [
        str(uid) for uid in User.objects.filter(is_verified=True, email__isnull=False)
        .values_list('id', flat=True)
    ]
    chunks = _chunked(user_ids, BULK_EMAIL_CHUNK_SIZE)

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

        # Дедуп-маркер для будущих прогонов (см. докстринг выше) — заодно
        # закрывает пробел с отсутствием in-app уведомления для этого типа.
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
    """
    Периодическая задача: собирает Notification (email_sent_at__isnull=True)
    типов new_badge/level_up/system по пользователям с email_digest_mode=True
    и шлёт одно письмо-сводку вместо N мгновенных.

    БАГ, КОТОРЫЙ ТУТ БЫЛ: периодическая задача (`crontab(minute=0)`, раз в
    час) без Redis-lock — при двух параллельных прогонах (плановый тик +
    повторная доставка сообщения at-least-once) оба могли прочитать один и
    тот же набор "ещё не отправленных" Notification ДО того, как первый
    прогон успеет проставить `email_sent_at`, и разослать дублирующие
    письма-сводки. Лок — тот же cache.add()-паттерн, что в
    season_squad/tasks.py::recompute_best_xi_task и
    notify_prediction_results выше.
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
                # Пользователь предпочитает мгновенные письма — дайджест их не трогает
                # (они уже были отправлены мгновенно и помечены email_sent_at при
                # создании — см. users/tasks.py, evaluations/views.py).
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
    Приглашение оценить только что завершённый матч — in-app + push + email.
    Ставится в очередь из `parsers/tasks.py::update_match_statuses` (и,
    подстраховкой, из `parsers/kff/importers.py::import_match_core`) через
    `transaction.on_commit` в момент первого перехода матча в 'finished'.

    РАСШИРЕНО (2026-09-01, прямая жалоба пользователя: email верифицирован,
    прогноз стоял, а пуша с приглашением оценить матч не пришло вообще).
    Раньше аудитория была ТОЛЬКО подписчики (`Follow`) на одну из играющих
    команд или на игрока в составе — так и было изначально задумано
    продуктом ("если человек подписан на команду или игроков"). На практике
    это означало, что пользователь, который просто поставил прогноз на матч
    (самый частый и очевидный кандидат на "пригласить оценить"), но не
    оформил отдельную Follow-подписку на команду, никогда не попадал в
    аудиторию — push для него не приходил не из-за бага, а по дизайну,
    который на практике ощущается как "не работает". Аудитория теперь —
    объединение (без дублей) подписчиков команд/игроков И всех, кто
    отправил `MatchPrediction` на этот матч.

    Раньше здесь ещё и разбирался мёртвый `send_voting_open_notification`
    (широковещательная email-рассылка ВСЕМ верифицированным пользователям,
    ни разу не вызывалась ни из кода, ни из CELERY_BEAT_SCHEDULE) — вместо
    того, чтобы оставлять его висеть как источник путаницы, функция удалена
    целиком (см. git-историю), а её часть аудитории (широковещательная)
    сознательно НЕ перенесена сюда: рассылать это буквально всем
    верифицированным пользователям при завершении КАЖДОГО матча тура — это
    email-спам для тех, кто вообще не интересовался этим конкретным матчем.
    Возврат к Follow + предсказавшим — таргетинг на тех, кому эта конкретная
    игра реально интересна.

    Email уважает пользовательскую настройку `email_match_finished`
    (см. NOTIFICATION_TYPE_TO_SETTINGS_KEY['voting_open']) — как и любой
    другой канал в этом модуле.
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

    # Push — лучшее из двух миров с in-app: следящий за игроком/предсказавший
    # пользователь часто НЕ сидит на сайте в момент финального свистка.
    # Best-effort: ошибка одного пользователя (устаревшая подписка и т.д.)
    # не должна прерывать рассылку остальным — см. try/except внутри
    # send_push_to_user самого по себе; здесь дополнительно оборачиваем весь
    # цикл на случай отсутствия pywebpush/VAPID-ключей в окружении.
    try:
        from notifications.services import send_push_to_user
        from users.models import User

        for user in User.objects.filter(id__in=audience_user_ids):
            send_push_to_user(user, title=title, body=message, url=action_url)
    except Exception as exc:
        logger.warning(f"notify_followers_match_activity: push fan-out skipped: {exc}")

    # Email — аудитория здесь (подписчики + предсказавшие на конкретный
    # матч) обычно единицы-десятки пользователей, поэтому шлём напрямую, без
    # chunked fan-out паттерна, которым пользуются широковещательные рассылки.
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


# Какие типы событий вообще стоят push-уведомления в реальном времени —
# см. докстринг notify_followers_match_event ниже. Вынесено на уровень
# модуля, чтобы parsers/tasks.py::update_match_statuses могло фильтровать
# ДО постановки задачи в очередь, не гоняя воркер зря на жёлтых карточках/
# заменах/сырых VAR-проверках без исхода.
PUSH_WORTHY_EVENT_TYPES = frozenset({'goal', 'own_goal', 'penalty', 'disallowed_goal', 'red_card'})


@shared_task(bind=True, max_retries=2)
def notify_followers_match_event(self, match_id: str, event_id: str):
    """
    Продуктовый аудит (2026-09-01, прямой запрос пользователя): live push
    ПО ХОДУ матча — гол/автогол/пенальти/отменённый (VAR) гол/красная
    карточка — подписчикам одной из играющих команд ИЛИ конкретного
    игрока, к которому относится событие. В отличие от
    `notify_followers_match_activity` (шлётся РОВНО ОДИН раз, в момент
    финального свистка, с приглашением оценить матч), эта задача может
    сработать много раз за один матч — по разу на каждое подходящее
    событие, см. `PUSH_WORTHY_EVENT_TYPES` выше и точку постановки в
    очередь — `parsers/tasks.py::update_match_statuses`, сразу после
    `import_events_and_minutes(..., on_event_created=...)`, СТРОГО для
    событий, которые только что реально впервые созданы (не для
    докрутки деталей у давно существующих).

    Только push + in-app, БЕЗ email — в отличие от голосования (редкое,
    важное событие, есть смысл слать письмо), гол по ходу матча — частый
    и мгновенный by design сигнал; письмо на каждый гол было бы спамом
    и пришло бы с опозданием, когда матч давно ушёл дальше.
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
        # Защита от рассинхрона id при вызове — не должно случаться в
        # нормальном потоке (event.match_id и есть match_id, которым
        # ставилась задача), но лучше явно отказаться, чем молча уведомить
        # не про тот матч.
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
    player_name = str(event.player) if event.player_id else None

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
        # PUSH_WORTHY_EVENT_TYPES фильтрует это на этапе постановки задачи —
        # сюда попасть не должно, но лучше тихо выйти, чем разослать
        # уведомление без осмысленного текста, если фильтр когда-нибудь
        # разойдётся с этим списком.
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

    # Push — best-effort, тот же паттерн, что notify_followers_match_activity
    # выше: сбой одной подписки/отсутствие VAPID-ключей не должен ронять
    # рассылку остальным подписчикам.
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

        subject = f'{match.home_team.name} — {match.away_team.name}: как думаете, кто победит?'
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

    БАГ, КОТОРЫЙ ТУТ БЫЛ: сама задача периодическая (`crontab(minute='*/30')`)
    и без Redis-lock — дедупликация по `Notification` (см. выше) защищает от
    задвоения ПОСЛЕ того, как `bulk_create` отработал, но не от гонки: два
    параллельных прогона (плановый тик + повторная доставка сообщения
    at-least-once) могли одновременно прочитать одну и ту же "ещё не
    уведомлённую" пару (match, user) ДО того, как один из них успеет создать
    Notification, и оба отправить письмо. Лок — тот же cache.add()-паттерн,
    что в season_squad/tasks.py::recompute_best_xi_task /
    round_squad/tasks.py::recompute_round_task.
    """
    lock_key = "notifications:lock:notify_prediction_results"
    if not cache.add(lock_key, "1", timeout=NOTIFY_TASK_LOCK_TIMEOUT):
        logger.info("notify_prediction_results: уже выполняется другим воркером — пропускаем")
        return {'notified': 0, 'skipped_locked': True}

    try:
        from django.urls import reverse

        from matches.models import Match
        from notifications.models import Notification
        from predictions.models import MatchPrediction
        from predictions.services import prediction_counts
        from users.tasks import check_and_award_badges_task

        now = timezone.now()
        lookback = now - timedelta(hours=6)

        # order_by('end_time') — ВАЖНО для серии прогнозов (см. блок ниже,
        # User.update_prediction_stats): если у пользователя в ОДНОМ прогоне
        # этой задачи сразу несколько свежезавершившихся матчей, +1/сброс
        # серии должны применяться в том порядке, в котором матчи реально
        # закончились, а не в произвольном порядке из БД.
        matches = Match.objects.filter(
            status='finished', end_time__isnull=False, end_time__gte=lookback, end_time__lte=now,
        ).select_related('home_team', 'away_team').order_by('end_time')

        result_labels = {'1': 'Победа хозяев', 'X': 'Ничья', '2': 'Победа гостей'}
        notified = 0

        for match in matches:
            # Дедуп по факту УСПЕШНОЙ отправки (email_sent_at), не по факту
            # создания записи — иначе сетевой сбой при отправке навсегда
            # блокирует переотправку. См.
            # docs/adr/0020-prediction-result-email-dedup.md.
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
                is_correct = pred.is_correct  # bool, т.к. match.final_result уже точно известен (status='finished')
                title = "✅ Ваш прогноз сбылся!" if is_correct else "Прогноз не сбылся"
                message = (
                    f"{match.home_team.name} {match.get_score_display()} {match.away_team.name} — "
                    f"{result_labels.get(match.final_result, '?')}. "
                    f"Ваш прогноз: {your_choice_labels.get(pred.choice, pred.choice)}."
                )
                existing = existing_by_user.get(pred.user_id)
                if existing:
                    # Строка от прошлого прогона, которому не удалось отправить
                    # письмо (email_sent_at пуст, иначе pred не попал бы сюда
                    # через already_emailed выше) — просто пробуем письмо снова.
                    # Серию НЕ трогаем повторно — она уже обновлена ниже, в
                    # ветке, где notification создаётся ВПЕРВЫЕ (см. коммент
                    # у update_prediction_stats() чуть ниже): иначе ретрай
                    # неудавшегося письма удвоил бы +1/сброс серии.
                    notif_by_user[pred.user_id] = existing
                    continue
                notif = Notification(
                    user=pred.user,
                    notification_type='prediction_result',
                    title=title,
                    message=message,
                    action_url=action_url,
                    related_match=match,
                    # email_sent_at НЕ проставляем здесь — только после
                    # реального успеха отправки ниже (см. докстринг-блок
                    # "БАГ, КОТОРЫЙ ТУТ БЫЛ" выше).
                )
                notifications_to_create.append(notif)
                notif_by_user[pred.user_id] = notif

                # Серия прогнозов (loop 4, "Серии") — обновляем РОВНО ОДИН
                # раз на результат, привязано к первому созданию этой
                # Notification (а не к успеху отправки письма — иначе
                # ретрай сорвавшегося письма удвоил бы счётчик, см. коммент
                # в ветке `if existing` выше). is_correct уже точно известен
                # (match.status == 'finished'), поэтому это безопасное место
                # для +1/сброса — единственный вызывающий
                # User.update_prediction_stats() во всём проекте.
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
    """
    2026-08-24, продуктовый запрос "хочу, чтобы модерация антифрода была
    максимально простой и не затратной по времени": раньше единственный
    способ узнать о новых флагах — самому не забыть зайти на
    /staff/dashboard/antifraud/. Теперь раз в неделю письмо с короткой
    сводкой само приходит на почту — не нужно ничего держать в голове.

    Считает то, что РЕАЛЬНО появилось за последние 7 дней (не всю
    вечно растущую очередь pending — иначе письмо распухнет и его
    перестанут читать), группирует по источнику, отдельно — топ-3 по
    score (самое подозрительное) и число открытых диспутов по рейтингу.
    Если за неделю не появилось вообще ничего нового — письмо не
    отправляется (см. `send_weekly_summary` выше — тот же принцип: "у вас
    0 всего" не несёт ценности).

    force=True — это операционное письмо для сотрудников, а не
    предпочтение пользователя, которое можно выключить через
    /notifications/settings/ (тех настроек для staff-ролей в проекте и
    нет).
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