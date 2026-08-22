# round_squad/tasks.py
"""
Периодический пересчёт «DOPX Лучшие тура» — тот же Redis-lock-паттерн, что
и season_squad/tasks.py (продуктовый ревью 2026-08-22 отдельно отметило
гонку при параллельном пересчёте одной и той же сущности как главный
риск для истории/консистентности денормализованных карточек). Плюс
fan-out рассылка письма с итогами тура (send_round_results_notification) —
тот же паттерн пачек, что notifications/tasks.py::send_voting_open_notification,
переиспользуем оттуда _send_email_to_user/_chunked/BULK_EMAIL_CHUNK_SIZE.
"""
from __future__ import annotations

import logging

from celery import shared_task
from django.core.cache import cache

logger = logging.getLogger(__name__)

# Тот же принцип, что RECOMPUTE_LOCK_TIMEOUT в season_squad/tasks.py —
# страховка на случай, если воркер упадёт посреди пересчёта одного тура.
ROUND_RECOMPUTE_LOCK_TIMEOUT = 300


@shared_task
def recompute_round_task(season_id: str, tour: int) -> None:
    """Пересчёт одного тура — отдельная задача (не инлайн-цикл в
    recompute_active_rounds), по тем же причинам, что у season_squad:
    зависание/ошибка на одном туре не блокирует остальные, ретраится
    Celery независимо. lock_key включает и сезон, и номер тура — два
    разных тура одного сезона пересчитываются параллельно без конфликта,
    гонка возможна только у ДВУХ прогонов ОДНОГО И ТОГО ЖЕ тура."""
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
def recompute_active_rounds() -> int:
    """Точка входа для Celery Beat — находит пары (сезон, тур), у которых
    есть хотя бы один завершённый матч, и RoundBestXI для которых ещё НЕ
    зафиксирован (is_final=False или ещё не существует), ставит по одной
    задаче на каждую. В отличие от season_squad.recompute_all_active_best_xi
    (там пересчитываются ВСЕ активные сезоны целиком каждый раз), здесь
    важно не пересчитывать бесконечно уже закрытые старые туры — set-разность
    finalized_pairs держит это дёшево даже к концу долгого сезона."""
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
    """Отправляет письмо одной пачке пользователей — тот же принцип, что
    notifications/tasks.py::_send_match_email_chunk. round_best_xi ГОТОВ и
    сохранён к моменту вызова (см. send_round_results_notification и
    round_squad/services.py::recompute_round, где .delay() ставится ПОСЛЕ
    .save())."""
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
    """
    Fan-out рассылка итогов тура — вызывается ОДИН раз из
    round_squad/services.py::recompute_round в момент, когда тур переходит
    в is_final=True (и из round_squad/admin.py::force_finalize при ручной
    фиксации стаффом). Тот же принцип, что notifications/tasks.py::
    send_voting_open_notification: широковещательно всем верифицированным
    пользователям с email, не только тем, кто голосовал за этот тур —
    итоги тура релевантны всей аудитории платформы, а не только
    участвовавшим (те же получатели, что у "Матч завершён").
    """
    from notifications.tasks import BULK_EMAIL_CHUNK_SIZE, _chunked
    from round_squad.models import RoundBestXI
    from users.models import User

    round_xi = RoundBestXI.objects.select_related('season').filter(id=round_best_xi_id).first()
    if not round_xi:
        logger.error("send_round_results_notification: RoundBestXI %s не найден", round_best_xi_id)
        return {'queued_chunks': 0, 'total_users': 0}

    subject = f'🏆 {round_xi.brand_title} готовы'
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
