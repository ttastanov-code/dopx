# users/tasks.py
"""
Celery-задачи для домена пользователей.

`check_and_award_badges_task` — асинхронный враппер над `users.services.
check_and_award_badges`, вызывается через `transaction.on_commit(...)` из
`evaluations/views.py::EvaluateMatchFinalView.form_valid`, не из HTTP-цикла.
Здесь же создаются in-app `Notification` о новых достижениях и ставятся в
очередь email-уведомления.

Дайджест: если у пользователя включён `email_digest_mode` (см.
`users/models.py::User.DEFAULT_NOTIFICATION_SETTINGS`), мгновенное письмо
не ставится в очередь — только `Notification` с `email_sent_at=None`,
которую подхватит `notifications/tasks.py::send_notification_digest`. Если
дайджест выключен — письмо уходит сразу, и `email_sent_at` проставляется
сразу же, чтобы дайджест не отправил его повторно.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger(__name__)

# Минимальное время (в секундах), за которое человек физически способен
# осмысленно пройти весь вайзард оценки (контекст → команды → до 22+
# игроков по 3 поля → тренеры → судья → финал). Меньше — сильный сигнал
# скрипта/бота, а не редкого быстрого человека.
MIN_HUMAN_WIZARD_SECONDS = 20

# IP-кластерный антифрод: сколько РАЗНЫХ аккаунтов, завершивших оценку
# ОДНОГО матча с ОДНОГО IP за LOOKBACK часов, считается подозрительным
# кластером (не "минимум 2" — соседи по квартире/офис/общага законно
# оценивают матчи с одного IP, порог должен ловить именно фермы).
IP_CLUSTER_LOOKBACK_HOURS = 24
IP_CLUSTER_MIN_ACCOUNTS = 3

# Бейдж «Чемпион месяца»: не выдаём тому, кто "занял первое место" с
# одной-двумя оценками в мёртвом месяце — минимальная активность для
# зачёта результата.
MONTHLY_CHAMPION_MIN_EVALUATIONS = 5


@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def check_and_award_badges_task(self, user_id: str, match_id: str | None = None) -> bool:
    """
    Проверяет достижения пользователя и уведомляет о новых.

    :param user_id: UUID пользователя строкой.
    :param match_id: UUID матча, в контексте которого выданы достижения
        (опционально — используется только для привязки уведомления к
        конкретному матчу через `Notification.related_match`).
    """
    from matches.models import Match
    from notifications.models import Notification
    from notifications.tasks import send_badge_earned_notification
    from users.models import User
    from users.services import check_and_award_badges

    try:
        user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        logger.error("check_and_award_badges_task: user %s not found", user_id)
        return False

    match = None
    if match_id:
        match = Match.objects.filter(id=match_id).only("id").first()

    try:
        awarded = check_and_award_badges(user)
    except Exception as exc:
        logger.error("check_and_award_badges_task failed for user %s: %s", user_id, exc, exc_info=True)
        raise self.retry(exc=exc)

    if not awarded:
        return True

    digest_mode = user.get_notification_setting("email_digest_mode", True)

    created_notifications = Notification.objects.bulk_create([
        Notification(
            user=user,
            notification_type="new_badge",
            title="🎖️ Новое достижение!",
            message=f"Вы получили достижение: {badge.get_badge_type_display()}",
            action_url="/users/profile/",
            is_read=False,
            related_match=match,
            # Если дайджест выключен, письмо уйдёт мгновенно ниже — сразу
            # помечаем как "отправлено", иначе send_notification_digest
            # разослала бы его ЕЩЁ РАЗ при следующем прогоне.
            email_sent_at=timezone.now() if not digest_mode else None,
        )
        for badge in awarded
    ])

    if not digest_mode:
        for badge in awarded:
            send_badge_earned_notification.delay(
                user_id=str(user.id),
                badge_type=badge.badge_type,
                badge_name=badge.get_badge_type_display(),
            )

    logger.info(
        "Awarded %d new badge(s) to user %s (%s)",
        len(awarded), user_id, "digest" if digest_mode else "instant email",
    )
    return True


@shared_task(bind=True, max_retries=3)
def award_founder_badge_if_eligible(self, user_id: str, founder_threshold: int = 500) -> bool:
    """
    Разовая проверка бейджа «Первопроходец» — вызывается ТОЛЬКО из
    `users/views.py::VerifyEmailView` в момент первой верификации email
    (не из `check_and_award_badges`, потому что это событие происходит один
    раз в жизни аккаунта, а не пересчитывается на каждой оценке).
    """
    from users.models import User, UserBadge

    try:
        user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        return False

    verified_rank = User.objects.filter(
        is_verified=True, date_joined__lte=user.date_joined
    ).count()
    if verified_rank > founder_threshold:
        return False

    badge, created = UserBadge.objects.get_or_create(user=user, badge_type="founder")
    if created:
        from notifications.models import Notification
        from notifications.tasks import send_badge_earned_notification

        digest_mode = user.get_notification_setting("email_digest_mode", True)
        Notification.objects.create(
            user=user,
            notification_type="new_badge",
            title="🎖️ Новое достижение!",
            message=f"Вы получили достижение: {badge.get_badge_type_display()}",
            action_url="/users/profile/",
            is_read=False,
            email_sent_at=timezone.now() if not digest_mode else None,
        )
        if not digest_mode:
            send_badge_earned_notification.delay(
                user_id=str(user.id), badge_type=badge.badge_type, badge_name=badge.get_badge_type_display()
            )
    return created


@shared_task
def flag_suspicious_wizard_speed_task(session_id: str) -> bool:
    """
    Антифрод-сигнал «слишком быстрое заполнение вайзарда» по
    `EvaluationSession.started_at`/`completed_at`.

    Не блокирует пользователя и не отменяет сохранённые оценки — только
    создаёт запись в очереди модерации (`SuspiciousActivityFlag`) с
    непрерывным скором; решение по флагу — за модератором, ложные
    срабатывания (быстрый, но настоящий пользователь) возможны.

    Вызывается через `transaction.on_commit(...)` из
    `evaluations/views.py::EvaluateMatchFinalView`, не блокирует ответ.
    """
    from evaluations.models import EvaluationSession
    from users.models import SuspiciousActivityFlag

    session = (
        EvaluationSession.objects.filter(id=session_id)
        .select_related("user", "match")
        .first()
    )
    if not session or session.status != "completed":
        return False

    duration = session.fill_duration_seconds
    if duration is None or duration >= MIN_HUMAN_WIZARD_SECONDS:
        return False

    # Чем короче время относительно порога — тем выше скор подозрительности.
    score = round(max(0.0, min(1.0, 1 - (duration / MIN_HUMAN_WIZARD_SECONDS))), 2)

    SuspiciousActivityFlag.objects.create(
        user=session.user,
        match=session.match,
        source="fast_wizard",
        score=score,
        details={
            "duration_seconds": round(duration, 2),
            "threshold_seconds": MIN_HUMAN_WIZARD_SECONDS,
            "session_id": str(session.id),
            "ip_address": session.ip_address,
        },
    )
    logger.warning(
        "Suspicious wizard speed flagged: user=%s match=%s duration=%.2fs score=%.2f",
        session.user_id, session.match_id, duration, score,
    )
    return True


@shared_task
def detect_ip_clusters_task() -> int:
    """
    Антифрод-сигнал «кластер аккаунтов с одного IP»: за последние
    `IP_CLUSTER_LOOKBACK_HOURS` часов ищет пары (матч, IP), с которых
    оценку завершили ≥`IP_CLUSTER_MIN_ACCOUNTS` РАЗНЫХ аккаунтов — сигнал
    накрутки голосования фермой аккаунтов.

    Один запрос ко всей таблице за окно + группировка в Python, не N
    запросов на кластер.

    Как и `flag_suspicious_wizard_speed_task`, никого не блокирует — только
    создаёт `SuspiciousActivityFlag(source="ip_cluster")` для ручного
    разбора модератором. Если по паре (пользователь, матч) уже есть
    неразобранный (`status="pending"`) флаг — новый не создаётся, чтобы
    периодический прогон не плодил дубли.
    """
    from collections import defaultdict

    from evaluations.models import EvaluationSession
    from users.models import SuspiciousActivityFlag

    since = timezone.now() - timedelta(hours=IP_CLUSTER_LOOKBACK_HOURS)

    rows = EvaluationSession.objects.filter(
        status="completed",
        completed_at__gte=since,
        ip_address__isnull=False,
    ).values_list("match_id", "ip_address", "user_id")

    clusters: dict[tuple, set] = defaultdict(set)
    for match_id, ip_address, user_id in rows:
        clusters[(match_id, ip_address)].add(user_id)

    flagged = 0
    for (match_id, ip_address), user_ids in clusters.items():
        account_count = len(user_ids)
        if account_count < IP_CLUSTER_MIN_ACCOUNTS:
            continue

        # Непрерывный скор: ровно на пороге — 0.5, дальше растёт до 1.0.
        score = round(min(1.0, account_count / (IP_CLUSTER_MIN_ACCOUNTS * 2)), 2)

        for user_id in user_ids:
            already_pending = SuspiciousActivityFlag.objects.filter(
                user_id=user_id, match_id=match_id, source="ip_cluster", status="pending"
            ).exists()
            if already_pending:
                continue

            SuspiciousActivityFlag.objects.create(
                user_id=user_id,
                match_id=match_id,
                source="ip_cluster",
                score=score,
                details={
                    "ip_address": ip_address,
                    "account_count": account_count,
                    "other_user_ids": [str(uid) for uid in user_ids if uid != user_id],
                    "lookback_hours": IP_CLUSTER_LOOKBACK_HOURS,
                },
            )
            flagged += 1

    if flagged:
        logger.warning(
            "IP-cluster antifraud: flagged %d account(s) across suspicious IP cluster(s).", flagged
        )
    return flagged


@shared_task
def award_monthly_champion_badge() -> bool:
    """
    Бейдж «Чемпион месяца»: разово (как и `founder`) выдаётся тому, кто
    завершил больше всех оценок за прошедший календарный месяц. Запускается
    1-го числа каждого месяца в 03:00 (см. `CELERY_BEAT_SCHEDULE` в
    `dopx/settings.py`).

    Метрика — количество завершённых `EvaluationSession` за месяц, не XP:
    `UserXP` хранит только текущий суммарный `total_xp`, отдельная
    помесячная таблица-леджер ради одной метрики избыточна, а завершённые
    оценки отражают ту же активность.

    Если лидер прошлого месяца уже получал этот бейдж — новый не выдаётся
    (статусный бейдж "было хотя бы раз", не помесячная история побед).
    """
    from django.db.models import Count

    from evaluations.models import EvaluationSession
    from notifications.models import Notification
    from notifications.tasks import send_badge_earned_notification
    from users.models import User, UserBadge

    now = timezone.now()
    first_of_this_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    first_of_prev_month = (first_of_this_month - timedelta(days=1)).replace(day=1)

    top = (
        EvaluationSession.objects.filter(
            status="completed",
            completed_at__gte=first_of_prev_month,
            completed_at__lt=first_of_this_month,
        )
        .values("user_id")
        .annotate(cnt=Count("id"))
        .filter(cnt__gte=MONTHLY_CHAMPION_MIN_EVALUATIONS)
        .order_by("-cnt")
        .first()
    )

    if not top:
        logger.info("award_monthly_champion_badge: недостаточно активности за прошлый месяц, бейдж не выдан.")
        return False

    user = User.objects.filter(id=top["user_id"]).first()
    if not user:
        return False

    badge, created = UserBadge.objects.get_or_create(user=user, badge_type="monthly_champion")
    if not created:
        logger.info(
            "award_monthly_champion_badge: %s снова лидер месяца (%d оценок), но бейдж уже выдавался ранее.",
            user.username, top["cnt"],
        )
        return False

    digest_mode = user.get_notification_setting("email_digest_mode", True)
    Notification.objects.create(
        user=user,
        notification_type="new_badge",
        title="🏆 Чемпион месяца!",
        message=f"Вы завершили больше всех оценок за прошлый месяц ({top['cnt']}) и получили достижение «Чемпион месяца»!",
        action_url="/users/profile/",
        is_read=False,
        email_sent_at=timezone.now() if not digest_mode else None,
    )
    if not digest_mode:
        send_badge_earned_notification.delay(
            user_id=str(user.id), badge_type=badge.badge_type, badge_name=badge.get_badge_type_display()
        )

    logger.info(
        "Чемпион месяца: %s (%d завершённых оценок за прошлый месяц).", user.username, top["cnt"]
    )
    return True