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

# --- Самокалибрующиеся антифрод-пороги (см. users/models.py::AntiFraudThreshold) ---

ANTIFRAUD_THRESHOLD_CACHE_TTL = 600  # секунд — не бить в БД на каждый вызов детектора

# Сколько разобранных (confirmed/dismissed) флагов нужно накопить за
# LOOKBACK, прежде чем на их основе вообще двигать порог — меньше
# статистически ничего не значит, порог в этот раз просто не трогаем.
ANTIFRAUD_RECALIBRATION_MIN_SAMPLE = 20
ANTIFRAUD_RECALIBRATION_LOOKBACK_DAYS = 90
# Ниже этой доли confirmed — сигнал в основном ложные тревоги, порог
# ужесточаем (менее чувствителен). Выше верхней — сигнал явно надёжный,
# порог смягчаем (ловим больше, раз почти всегда попадаем в цель).
ANTIFRAUD_RECALIBRATION_LOW_CONFIRM_RATE = 0.2
ANTIFRAUD_RECALIBRATION_HIGH_CONFIRM_RATE = 0.8

# 2026-08-24, продуктовый запрос "модерация антифрода должна быть
# максимально простой и не затратной по времени" — см.
# expire_stale_low_score_flags() ниже. Источники, которые ВСЕГДА требуют
# явного решения человека, никогда не авто-закрываются: vote_spike/
# ip_cluster — единственные два сигнала, у которых решение модератора
# ЕЩЁ И кормит самокалибровку выше (без решения calibration застаивается),
# а "manual" — флаг, который человек и так завёл сам, тихо его закрыть
# значило бы просто проигнорировать то, что сотрудник явно отметил.
ANTIFRAUD_AUTO_EXPIRE_EXCLUDED_SOURCES = ("vote_spike", "ip_cluster", "manual")
# "Низкий score" — тот же порог, что уже используется в UI очереди
# (templates/dashboard/antifraud.html — ниже него бейдж серый/"ghost", не
# жёлтый и не красный) — не придумываем новую границу, используем ту, что
# сотрудник и так визуально считает "неважным".
ANTIFRAUD_AUTO_EXPIRE_MAX_SCORE = 0.4
ANTIFRAUD_AUTO_EXPIRE_AFTER_DAYS = 14

# Реестр калибруемых порогов: ключ в БД -> источник флагов для обратной
# связи, шаг одной корректировки и жёсткая вилка (min/max), за которую
# калибровка не может выйти. default совпадает со старой константой,
# которая жила здесь/в aggregates/tasks.py до самокалибровки — это
# стартовая точка, а не потолок.
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
        # Нижняя граница НЕ 2 — умышленно, см. докстринг IP_CLUSTER_MIN_ACCOUNTS
        # выше: порог "минимум 2" ловит законных соседей по IP (общага/офис).
        "min": 3.0,
        "max": 6.0,
        "default": float(IP_CLUSTER_MIN_ACCOUNTS),
    },
}


def get_antifraud_threshold(key: str, default: float) -> float:
    """
    Текущее (возможно, уже откалиброванное) значение антифрод-порога.
    Читает `AntiFraudThreshold` с коротким кэшем — вызывается на каждый
    прогон детектора (`detect_ip_clusters_task`, `aggregates.tasks.
    detect_vote_velocity_anomalies_task`), поэтому лишний SELECT на каждый
    вызов был бы расточительным.

    При отсутствии строки в БД (порог этого ключа ещё ни разу не
    калибровался) возвращает `default`, ничего не создавая и не трогая
    БД — инициализация строки принадлежит `recalibrate_antifraud_thresholds`
    (или ручному вводу в admin), а не побочному эффекту чтения.
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
    # Самокалибрующийся порог (см. recalibrate_antifraud_thresholds) —
    # IP_CLUSTER_MIN_ACCOUNTS остаётся значением по умолчанию/нижней
    # границей вилки калибровки, а не обязательным действующим числом.
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

        # Непрерывный скор: ровно на пороге — 0.5, дальше растёт до 1.0.
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
                    "lookback_hours": IP_CLUSTER_LOOKBACK_HOURS,
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


@shared_task
def recalibrate_antifraud_thresholds() -> dict:
    """
    Еженедельная самокалибровка порогов vote_spike/ip_cluster на основе
    ФАКТИЧЕСКИХ решений модератора (confirmed/dismissed за последние
    `ANTIFRAUD_RECALIBRATION_LOOKBACK_DAYS` дней), см. докстринг
    `users.models.AntiFraudThreshold`:

    - Доля confirmed низкая (детектор в основном создаёт ложные тревоги)
      → порог сдвигается в сторону "строже" (менее чувствительно).
    - Доля confirmed высокая (сигнал явно надёжный) → порог сдвигается в
      сторону "чувствительнее" (можно ловить больше, раз почти всегда
      попадаем в цель).
    - Решений меньше `ANTIFRAUD_RECALIBRATION_MIN_SAMPLE` — калибровка
      этого порога в этот раз пропускается, ничего не трогаем: на
      маленькой выборке confirm_rate статистически ничего не значит.

    Жёсткие min/max в `ANTIFRAUD_CALIBRATED_THRESHOLDS` не дают уйти в
    бессмысленную/опасную зону, даже если решения модератора смещены.
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
    """
    2026-08-24, продуктовый запрос "модерация антифрода должна быть
    максимально простой и не затратной по времени": очередь `pending`
    иначе только растёт — старые слабые сигналы, на которые никто не
    отреагировал, годами висят и создают ложное ощущение "накопился
    большой долг", хотя реальной ценности в их разборе уже нет.

    Раз в сутки автоматически закрывает флаги, которые ОДНОВРЕМЕННО:
    - старше ANTIFRAUD_AUTO_EXPIRE_AFTER_DAYS дней;
    - со score ниже ANTIFRAUD_AUTO_EXPIRE_MAX_SCORE (в UI такие и так
      серые/"неважные", не жёлтые/красные);
    - источник НЕ в ANTIFRAUD_AUTO_EXPIRE_EXCLUDED_SOURCES (vote_spike/
      ip_cluster всегда ждут явного решения человека — оно ещё и кормит
      самокалибровку; manual — человек завёл сам).

    Статус становится "dismissed", но НЕ как решение модератора — reviewed_by
    остаётся None, а в `details` добавляется явная пометка `auto_expired`,
    чтобы в CSV-экспорте/аудите было видно: это была автоматическая уборка,
    а не чья-то оценка "это ложное срабатывание".
    """
    from users.models import SuspiciousActivityFlag

    cutoff = timezone.now() - timedelta(days=ANTIFRAUD_AUTO_EXPIRE_AFTER_DAYS)
    stale = SuspiciousActivityFlag.objects.filter(
        status="pending",
        score__lt=ANTIFRAUD_AUTO_EXPIRE_MAX_SCORE,
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