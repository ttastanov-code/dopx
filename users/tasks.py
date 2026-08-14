# users/tasks.py
"""
НОВЫЙ ФАЙЛ. Celery-задачи для домена пользователей.

`check_and_award_badges_task` — асинхронный враппер над `users.services.
check_and_award_badges`, вынесенный из синхронного HTTP-запроса (см.
докстринг `users/services.py`, пункт 3, и `evaluations/views.py::
EvaluateMatchFinalView.form_valid`, откуда теперь ставится в очередь через
`transaction.on_commit(...)` вместо прямого вызова).

Здесь же, а не в `evaluations/views.py`, теперь создаются in-app
`Notification` о новых достижениях и ставятся в очередь email-уведомления —
раньше это тоже происходило синхронно в HTTP-запросе.

ДАЙДЖЕСТ: если у пользователя включён `email_digest_mode` (по умолчанию —
да, см. `users/models.py::User.DEFAULT_NOTIFICATION_SETTINGS`), мгновенное
письмо о достижении здесь НЕ ставится в очередь — только создаётся in-app
`Notification` с `email_sent_at=None`, которую позже подхватит и разошлёт
`notifications/tasks.py::send_notification_digest`. Если дайджест выключен —
письмо уходит мгновенно, как раньше, и `email_sent_at` проставляется сразу,
чтобы дайджест не отправил его повторно.
"""
from __future__ import annotations

import logging

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger(__name__)

# Минимальное время (в секундах), за которое человек физически способен
# осмысленно пройти весь вайзард оценки (контекст → команды → до 22+
# игроков по 3 поля → тренеры → судья → финал). Меньше — сильный сигнал
# скрипта/бота, а не редкого быстрого человека.
MIN_HUMAN_WIZARD_SECONDS = 20


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
    Антифрод-сигнал «слишком быстрое заполнение вайзарда» (продуктовый
    аудит, раздел 4.3). Данные для этого — `EvaluationSession.started_at`/
    `completed_at` — уже существовали в схеме и раньше никак не
    использовались как сигнал.

    Не блокирует пользователя и не отменяет уже сохранённые оценки — только
    создаёт запись в очереди модерации (`SuspiciousActivityFlag`) с непрерывным
    скором. Ложные срабатывания возможны (очень быстрый, но настоящий
    пользователь) — поэтому решение остаётся за модератором, а не за кодом.

    Асинхронная задача: вызывается через `transaction.on_commit(...)` из
    `evaluations/views.py::EvaluateMatchFinalView`, не блокирует ответ
    пользователю.
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