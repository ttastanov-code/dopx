# users/tasks.py
"""Celery-задачи пользователей: бейджи, антифрод-детекторы, decay trust_score, калибровка порогов.

При включённом email_digest_mode письмо не шлём сразу — его заберёт
notifications.tasks.send_notification_digest.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger(__name__)

# Минимальное реальное время прохождения вайзарда. Быстрее — похоже на бота.
MIN_HUMAN_WIZARD_SECONDS = 20

# Сколько разных аккаунтов с одного IP на один матч за окно считаем кластером.
# Не 2 — соседи/офис легально голосуют с одного IP.
IP_CLUSTER_LOOKBACK_HOURS = 24
IP_CLUSTER_MIN_ACCOUNTS = 3

# Доля расстояния до trust_score=1.0, на которую сдвигаем активного пользователя за прогон decay.
TRUST_DECAY_FRACTION = 0.1
# Неактивных не трогаем — иначе «реабилитация» без новых голосов.
TRUST_DECAY_LOOKBACK_DAYS = 90

# Минимум оценок за месяц для бейджа «Чемпион месяца».
MONTHLY_CHAMPION_MIN_EVALUATIONS = 5

# --- Самокалибрующиеся пороги (users/models.py::AntiFraudThreshold) ---

ANTIFRAUD_THRESHOLD_CACHE_TTL = 600  # кэш, сек

# Минимум разобранных флагов за окно, чтобы двигать порог.
ANTIFRAUD_RECALIBRATION_MIN_SAMPLE = 20
ANTIFRAUD_RECALIBRATION_LOOKBACK_DAYS = 90
# Доля confirmed ниже LOW — ужесточаем порог, выше HIGH — смягчаем.
ANTIFRAUD_RECALIBRATION_LOW_CONFIRM_RATE = 0.2
ANTIFRAUD_RECALIBRATION_HIGH_CONFIRM_RATE = 0.8

# Эти источники никогда не закрываем автоматически:
# vote_spike/ip_cluster кормят калибровку, manual завёл человек.
ANTIFRAUD_AUTO_EXPIRE_EXCLUDED_SOURCES = ("vote_spike", "ip_cluster", "manual")
# Порог «низкого» score — тот же, что в UI очереди (серый бейдж).
ANTIFRAUD_AUTO_EXPIRE_MAX_SCORE = 0.4
ANTIFRAUD_AUTO_EXPIRE_AFTER_DAYS = 14

# Калибруемые пороги: ключ -> источник флагов, шаг, вилка min/max.
# default — стартовое значение.
ANTIFRAUD_CALIBRATED_THRESHOLDS = {
    "vote_spike_mad_threshold": {
        "source": "vote_spike",
        "step": 0.25,
        "min": 3.0,
        "max": 5.0,
        "default": 3.5,
    },
    "ip_cluster_min_accounts": {
        "source": "ip_cluster",
        "step": 1.0,
        # Нижняя граница не 2 — см. IP_CLUSTER_MIN_ACCOUNTS.
        "min": 3.0,
        "max": 6.0,
        "default": float(IP_CLUSTER_MIN_ACCOUNTS),
    },
    # Калибруется порог видимости флага extreme_bias, а не сама формула штрафа.
    "extreme_bias_flag_threshold": {
        "source": "extreme_bias",
        "step": 0.05,
        "min": 0.1,
        "max": 0.35,
        "default": 0.2,
    },
}


def get_antifraud_threshold(key: str, default: float) -> float:
    """Текущее значение антифрод-порога (с кэшем).
    Если строки в БД нет — возвращает default, ничего не создаёт.
    """
    from django.core.cache import cache

    cache_key = f"antifraud_threshold:{key}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    from users.models import AntiFraudThreshold

    row = AntiFraudThreshold.objects.filter(key=key).only("value").first()
    value = row.value if row else default
    cache.set(cache_key, value, timeout=ANTIFRAUD_THRESHOLD_CACHE_TTL)
    return value


@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def check_and_award_badges_task(self, user_id: str, match_id: str | None = None) -> bool:
    """Проверяет достижения пользователя и создаёт уведомления о новых.

    :param user_id: UUID пользователя.
    :param match_id: UUID матча для привязки уведомления (опционально).
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
            # Письмо уйдёт сразу — помечаем отправленным, чтобы дайджест не продублировал.
            email_sent_at=timezone.now() if not digest_mode else None,
        )
        for badge in awarded
    ])

    from notifications.tasks import _push_fan_out

    names = ", ".join(str(b.get_badge_type_display()) for b in awarded)
    _push_fan_out(
        [user.id],
        "🎖️ Новое достижение!" if len(awarded) == 1 else f"🎖️ Новые достижения: {len(awarded)}",
        names, "/users/profile/", kind="achievement", tag=f"badge-{user.id}",
    )

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
    """Бейдж «Первопроходец» — вызывается из VerifyEmailView при первой верификации.
    Ранг считаем по date_joined среди всех аккаунтов, а не только верифицированных.
    """
    from users.models import User, UserBadge

    try:
        user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        return False

    registration_rank = User.objects.filter(date_joined__lte=user.date_joined).count()
    if registration_rank > founder_threshold:
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
    """Флаг «слишком быстрое заполнение вайзарда».
    Никого не блокирует — только создаёт SuspiciousActivityFlag для модерации.
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

    # Порог — из настроек платформы, константа как fallback.
    from core.models import get_setting
    threshold = get_setting("fast_wizard_min_seconds", MIN_HUMAN_WIZARD_SECONDS)

    duration = session.fill_duration_seconds
    if duration is None or duration >= threshold:
        return False

    score = round(max(0.0, min(1.0, 1 - (duration / threshold))), 2)

    SuspiciousActivityFlag.objects.create(
        user=session.user,
        match=session.match,
        source="fast_wizard",
        score=score,
        details={
            "duration_seconds": round(duration, 2),
            "threshold_seconds": threshold,
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
    """Флаг «кластер аккаунтов с одного IP» за последние IP_CLUSTER_LOOKBACK_HOURS.
    Один запрос + группировка в Python. Дубли pending-флагов не создаём.
    """
    from collections import defaultdict

    from core.models import get_setting
    from evaluations.models import EvaluationSession
    from users.models import SuspiciousActivityFlag

    lookback_hours = get_setting("ip_cluster_lookback_hours", IP_CLUSTER_LOOKBACK_HOURS)
    since = timezone.now() - timedelta(hours=lookback_hours)
    # Порог самокалибрующийся, константа — дефолт.
    min_accounts = get_antifraud_threshold(
        "ip_cluster_min_accounts", ANTIFRAUD_CALIBRATED_THRESHOLDS["ip_cluster_min_accounts"]["default"]
    )

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
        if account_count < min_accounts:
            continue

        # Скор: на пороге 0.5, дальше растёт до 1.0.
        score = round(min(1.0, account_count / (min_accounts * 2)), 2)

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
                    "lookback_hours": lookback_hours,
                    "threshold_used": min_accounts,
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
    """Бейдж «Чемпион месяца» — лидеру по завершённым оценкам за прошлый месяц.
    Запуск 1-го числа в 03:00. Повторно одному и тому же не выдаётся.
    """
    from django.db.models import Count

    from core.models import get_setting
    from evaluations.models import EvaluationSession
    from notifications.models import Notification
    from notifications.tasks import send_badge_earned_notification
    from users.models import User, UserBadge

    min_evaluations = get_setting("monthly_champion_min_evaluations", MONTHLY_CHAMPION_MIN_EVALUATIONS)
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
        .filter(cnt__gte=min_evaluations)
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


# За один прогон — не больше стольких сессий (остальные на следующем).
TRUST_SETTLE_BATCH_SIZE = 500


@shared_task
def settle_trust_scores_task() -> int:
    """Trust score по завершённым оценкам матчей, где голосование уже закрыто:
    сравнение с итоговым консенсусом (aggregates.services.calculate_user_trust_adjustment).
    """
    from django.db import transaction

    from aggregates.services import calculate_user_trust_adjustment
    from evaluations.models import EvaluationSession
    from users.models import User

    now = timezone.now()
    sessions = list(
        EvaluationSession.objects.filter(
            status="completed", trust_settled_at__isnull=True, match__voting_open_until__lt=now,
        ).select_related("match").order_by("completed_at")[:TRUST_SETTLE_BATCH_SIZE]
    )
    settled = 0
    for session in sessions:
        with transaction.atomic():
            locked = EvaluationSession.objects.select_for_update().get(pk=session.pk)
            if locked.trust_settled_at is not None:
                continue
            user = User.objects.select_for_update().get(pk=session.user_id)
            if user.is_active:
                adjustment = calculate_user_trust_adjustment(user, session.match)
                new_trust = max(0.5, min(2.0, user.trust_score + adjustment))
                if abs(new_trust - user.trust_score) >= 0.001:
                    User.objects.filter(pk=user.pk).update(trust_score=new_trust)
            locked.trust_settled_at = now
            locked.save(update_fields=["trust_settled_at", "updated_at"])
        settled += 1
    if settled:
        logger.info("settle_trust_scores_task: учтено %d сессий.", settled)
    return settled


@shared_task
def decay_trust_scores_task() -> int:
    """Раз в месяц тянет trust_score активных пользователей к 1.0.
    Шаг — доля расстояния до 1.0 (TRUST_DECAY_RATE), работает в обе стороны.
    Неактивных за TRUST_DECAY_LOOKBACK_DAYS не трогаем.
    """
    from core.models import get_setting
    from evaluations.models import EvaluationSession
    from users.models import User

    fraction = get_setting("trust_decay_fraction", TRUST_DECAY_FRACTION)
    lookback_days = get_setting("trust_decay_lookback_days", TRUST_DECAY_LOOKBACK_DAYS)
    cutoff = timezone.now() - timedelta(days=lookback_days)

    active_user_ids = (
        EvaluationSession.objects.filter(status="completed", completed_at__gte=cutoff)
        .values_list("user_id", flat=True)
        .distinct()
    )

    updated = 0
    for user in User.objects.filter(id__in=active_user_ids).exclude(trust_score=1.0).only("id", "trust_score"):
        new_score = 1.0 + (user.trust_score - 1.0) * (1 - fraction)
        new_score = max(0.5, min(2.0, new_score))
        if abs(new_score - user.trust_score) < 0.001:
            continue
        User.objects.filter(id=user.id).update(trust_score=new_score)
        updated += 1

    logger.info(
        "decay_trust_scores_task: trust_score сдвинут к нейтральному у %d активных пользователей (шаг %.0f%% расстояния до 1.0).",
        updated, fraction * 100,
    )
    return updated


@shared_task
def revalidate_status_badges_task() -> dict:
    """Раз в месяц перепроверяет статусные бейджи и ставит is_stale, если показатель упал.
    Идём только по владельцам таких бейджей.
    """
    from users.models import User, UserBadge
    from users.services import STATUS_BADGE_TYPES, revalidate_status_badges

    user_ids = (
        UserBadge.objects.filter(badge_type__in=STATUS_BADGE_TYPES)
        .values_list("user_id", flat=True)
        .distinct()
    )

    total_now_stale = 0
    total_reactivated = 0
    affected_users = 0

    for user in User.objects.filter(id__in=user_ids):
        result = revalidate_status_badges(user)
        if not result["now_stale"] and not result["reactivated"]:
            continue
        affected_users += 1
        total_now_stale += len(result["now_stale"])
        total_reactivated += len(result["reactivated"])

        if result["now_stale"]:
            _notify_status_badges_stale(user, result["now_stale"])
        if result["reactivated"]:
            _notify_status_badges_reactivated(user, result["reactivated"])

    logger.info(
        "revalidate_status_badges_task: %d бейдж(ей) помечены устаревшими, %d возвращены в силу, "
        "затронуто %d пользователей.",
        total_now_stale, total_reactivated, affected_users,
    )
    return {"now_stale": total_now_stale, "reactivated": total_reactivated, "affected_users": affected_users}


def _notify_status_badges_stale(user, badge_types: list[str]) -> None:
    """Мягкое уведомление — бейдж остаётся, просто помечен."""
    from notifications.models import Notification
    from users.badges import get_badge_definition

    names = ", ".join(
        get_badge_definition(bt).name if get_badge_definition(bt) else bt for bt in badge_types
    )
    Notification.objects.create(
        user=user,
        notification_type="new_badge",
        title="Статус достижения обновлён",
        message=(
            f"Показатель для «{names}» временно опустился ниже порога — достижение осталось в вашем "
            f"профиле как исторический факт, но помечено как неактуальное. Вернётся в силу автоматически, "
            f"если показатель снова достигнет порога."
        ),
        action_url="/users/profile/",
        is_read=False,
    )


def _notify_status_badges_reactivated(user, badge_types: list[str]) -> None:
    from notifications.models import Notification
    from users.badges import get_badge_definition

    names = ", ".join(
        get_badge_definition(bt).name if get_badge_definition(bt) else bt for bt in badge_types
    )
    Notification.objects.create(
        user=user,
        notification_type="new_badge",
        title="Достижение снова в силе",
        message=f"Показатель для «{names}» снова достиг порога — пометка «неактуально» снята.",
        action_url="/users/profile/",
        is_read=False,
    )


@shared_task
def recalibrate_antifraud_thresholds() -> dict:
    """Еженедельная калибровка порогов по решениям модератора за окно.
    Мало confirmed — ужесточаем, много — смягчаем. Мало решений — пропускаем.
    Вилка min/max не даёт уйти в опасную зону.
    """
    from users.models import AntiFraudThreshold, SuspiciousActivityFlag

    since = timezone.now() - timedelta(days=ANTIFRAUD_RECALIBRATION_LOOKBACK_DAYS)
    results: dict = {}

    for key, cfg in ANTIFRAUD_CALIBRATED_THRESHOLDS.items():
        resolved = SuspiciousActivityFlag.objects.filter(
            source=cfg["source"], status__in=["confirmed", "dismissed"], reviewed_at__gte=since,
        )
        total = resolved.count()
        if total < ANTIFRAUD_RECALIBRATION_MIN_SAMPLE:
            results[key] = {"skipped": True, "reason": "insufficient_sample", "sample": total}
            continue

        confirmed = resolved.filter(status="confirmed").count()
        confirm_rate = confirmed / total

        row, _created = AntiFraudThreshold.objects.get_or_create(
            key=key,
            defaults={
                "value": cfg["default"],
                "default_value": cfg["default"],
                "min_value": cfg["min"],
                "max_value": cfg["max"],
            },
        )

        old_value = row.value
        if confirm_rate < ANTIFRAUD_RECALIBRATION_LOW_CONFIRM_RATE:
            new_value = min(row.value + cfg["step"], row.max_value)
            note = f"confirm_rate={confirm_rate:.2f} низкий (порог ужесточён)"
        elif confirm_rate > ANTIFRAUD_RECALIBRATION_HIGH_CONFIRM_RATE:
            new_value = max(row.value - cfg["step"], row.min_value)
            note = f"confirm_rate={confirm_rate:.2f} высокий (порог смягчён)"
        else:
            new_value = row.value
            note = f"confirm_rate={confirm_rate:.2f} в норме (без изменений)"

        if new_value != old_value:
            from django.core.cache import cache

            row.value = new_value
            row.last_note = note
            row.save(update_fields=["value", "last_note", "updated_at"])
            cache.delete(f"antifraud_threshold:{key}")
            logger.info(
                "Antifraud threshold recalibrated: %s %.2f -> %.2f (%s)", key, old_value, new_value, note,
            )

        results[key] = {
            "sample": total, "confirm_rate": round(confirm_rate, 2), "old": old_value, "new": new_value,
        }

    return results


@shared_task
def expire_stale_low_score_flags() -> int:
    """Раз в сутки закрывает старые слабые флаги (возраст > AUTO_EXPIRE_AFTER_DAYS,
    score < AUTO_EXPIRE_MAX_SCORE, источник не из EXCLUDED_SOURCES).
    Статус dismissed, reviewed_by=None, в details пометка auto_expired.
    """
    from core.models import get_setting
    from users.models import SuspiciousActivityFlag

    after_days = get_setting("antifraud_auto_expire_after_days", ANTIFRAUD_AUTO_EXPIRE_AFTER_DAYS)
    max_score = get_setting("antifraud_auto_expire_max_score", ANTIFRAUD_AUTO_EXPIRE_MAX_SCORE)
    cutoff = timezone.now() - timedelta(days=after_days)
    stale = SuspiciousActivityFlag.objects.filter(
        status="pending",
        score__lt=max_score,
        created_at__lt=cutoff,
    ).exclude(source__in=ANTIFRAUD_AUTO_EXPIRE_EXCLUDED_SOURCES)

    expired = 0
    for flag in stale:
        details = dict(flag.details or {})
        details["auto_expired"] = True
        details["auto_expired_at"] = timezone.now().isoformat()
        flag.details = details
        flag.status = "dismissed"
        flag.reviewed_at = timezone.now()
        flag.save(update_fields=["status", "details", "reviewed_at", "updated_at"])
        expired += 1

    if expired:
        logger.info("expire_stale_low_score_flags: auto-closed %d stale low-score flag(s).", expired)
    return expired

# Пачка удалений (все сессии разом) -> один пересчёт через несколько секунд.
PROGRESS_RECOMPUTE_DELAY = 10


def schedule_progress_recompute(user_id: str) -> None:
    from django.core.cache import cache

    if cache.add(f"users:progress_recompute:{user_id}", 1, PROGRESS_RECOMPUTE_DELAY):
        recompute_user_progress_task.apply_async(args=[user_id], countdown=PROGRESS_RECOMPUTE_DELAY)


@shared_task(bind=True, max_retries=3, acks_late=True, reject_on_worker_lost=True)
def recompute_user_progress_task(self, user_id: str) -> dict | None:
    """Пересчёт XP, серий и достижений (users/progress.py)."""
    from users.models import User
    from users.progress import recompute_user_progress

    user = User.objects.filter(pk=user_id).first()
    if user is None:
        return None
    return recompute_user_progress(user)
