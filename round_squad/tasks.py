# round_squad/tasks.py
"""Пересчёт «DOPX Лучшие тура» с Redis-lock и рассылка итогов тура пачками."""
from __future__ import annotations

import logging

from celery import shared_task
from django.core.cache import cache

logger = logging.getLogger(__name__)

# TTL лока пересчёта тура.
ROUND_RECOMPUTE_LOCK_TIMEOUT = 300

# TTL лока рассылки итогов.
ROUND_NOTIFY_LOCK_TIMEOUT = 600

# TTL лока пересчёта всех закрытых туров — с запасом.
ALL_CLOSED_ROUNDS_LOCK_TIMEOUT = 1800


@shared_task
def recompute_round_task(season_id: str, tour: int) -> None:
    """Пересчёт одного тура. Лок по (сезон, тур)."""
    lock_key = f"round_squad:recompute:{season_id}:{tour}"
    if not cache.add(lock_key, "1", timeout=ROUND_RECOMPUTE_LOCK_TIMEOUT):
        logger.info("recompute_round_task: тур %s сезона %s уже пересчитывается — пропускаем", tour, season_id)
        return

    try:
        from seasons.models import Season
        from round_squad.services import recompute_round

        try:
            season = Season.objects.select_related('league').get(pk=season_id)
        except Season.DoesNotExist:
            logger.warning("recompute_round_task: сезон %s не найден (удалён?)", season_id)
            return

        recompute_round(season, tour)
    finally:
        cache.delete(lock_key)


@shared_task
def recompute_all_closed_rounds_task() -> int:
    """Пересчёт всех зафиксированных туров (кнопка в дашборде). Один лок на всю операцию."""
    lock_key = "round_squad:recompute_all_closed:running"
    if not cache.add(lock_key, "1", timeout=ALL_CLOSED_ROUNDS_LOCK_TIMEOUT):
        logger.info("recompute_all_closed_rounds_task: уже выполняется — пропускаем")
        return 0
    try:
        from round_squad.services import recompute_all_closed_rounds

        return recompute_all_closed_rounds()
    finally:
        cache.delete(lock_key)


@shared_task
def recompute_active_rounds() -> int:
    """Для Celery Beat: ставит пересчёт каждого незафиксированного тура с завершёнными матчами."""
    from matches.models import Match
    from round_squad.models import RoundBestXI

    candidate_pairs = set(
        Match.objects.filter(season__is_active=True, tour__isnull=False, status='finished')
        .values_list('season_id', 'tour').distinct()
    )
    finalized_pairs = set(
        RoundBestXI.objects.filter(is_final=True).values_list('season_id', 'tour')
    )
    pending = candidate_pairs - finalized_pairs

    for season_id, tour in pending:
        recompute_round_task.delay(str(season_id), tour)

    logger.info("recompute_active_rounds: поставлено %d задач пересчёта туров", len(pending))
    return len(pending)


@shared_task(bind=True, max_retries=3, rate_limit='60/m')
def _send_round_results_email_chunk(self, user_ids: list[str], round_best_xi_id: str, subject: str) -> int:
    """Письма одной пачке пользователей."""
    from notifications.tasks import _send_email_to_user
    from round_squad.models import RoundBestXI
    from users.models import User

    round_xi = (
        RoundBestXI.objects
        .select_related('season', 'most_dramatic_match__home_team', 'most_dramatic_match__away_team')
        .filter(id=round_best_xi_id).first()
    )
    if not round_xi:
        logger.error("_send_round_results_email_chunk: RoundBestXI %s не найден", round_best_xi_id)
        return 0

    sent = 0
    users = User.objects.filter(id__in=user_ids, is_verified=True, email__isnull=False)
    for user in users:
        if _send_email_to_user(
            user, subject, 'emails/round_results.html', {'round_xi': round_xi}, notification_type='round_results',
        ):
            sent += 1
    return sent


@shared_task(bind=True, max_retries=3, countdown=5)
def send_round_results_notification(self, round_best_xi_id: str) -> dict:
    """Рассылка итогов тура всем верифицированным — при финализации тура
    (recompute_round или force_finalize в админке). Лок по round_best_xi_id от дублей.
    """
    from notifications.tasks import BULK_EMAIL_CHUNK_SIZE, _chunked
    from round_squad.models import RoundBestXI
    from users.models import User

    lock_key = f"round_squad:notify:{round_best_xi_id}"
    if not cache.add(lock_key, "1", timeout=ROUND_NOTIFY_LOCK_TIMEOUT):
        logger.info(
            "send_round_results_notification: рассылка для RoundBestXI %s уже поставлена в очередь — пропускаем",
            round_best_xi_id,
        )
        return {'queued_chunks': 0, 'total_users': 0, 'skipped_locked': True}

    try:
        round_xi = RoundBestXI.objects.select_related('season').filter(id=round_best_xi_id).first()
        if not round_xi:
            logger.error("send_round_results_notification: RoundBestXI %s не найден", round_best_xi_id)
            return {'queued_chunks': 0, 'total_users': 0}

        subject = f'{round_xi.brand_title} готовы'
        user_ids = [
            str(uid) for uid in User.objects.filter(is_verified=True, email__isnull=False).values_list('id', flat=True)
        ]

        chunks = _chunked(user_ids, BULK_EMAIL_CHUNK_SIZE)
        for chunk in chunks:
            _send_round_results_email_chunk.delay(chunk, str(round_xi.id), subject)

        logger.info(
            "send_round_results_notification: поставлено %d пачек (%d пользователей) для %s",
            len(chunks), len(user_ids), round_xi.brand_title,
        )
        return {'queued_chunks': len(chunks), 'total_users': len(user_ids)}
    finally:
        # Снимаем лок сразу, не ждём TTL.
        cache.delete(lock_key)
