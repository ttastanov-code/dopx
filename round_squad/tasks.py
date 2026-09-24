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


def notify_round_xi_followers(round_xi) -> int:
    """In-app + push подписчикам игроков сборной тура и их команд.
    Дедуп — по Notification round_results с тем же action_url.
    """
    from django.contrib.contenttypes.models import ContentType
    from django.urls import reverse

    from notifications.models import Notification
    from notifications.tasks import _push_fan_out
    from players.models import Player
    from users.models import Follow

    player_ct = ContentType.objects.get_for_model(Player)
    player_ids = list(
        round_xi.slots.filter(content_type=player_ct, object_id__isnull=False).values_list('object_id', flat=True)
    )
    if not player_ids:
        return 0

    players = {p.id: p for p in Player.objects.filter(id__in=player_ids).select_related('team')}
    team_players: dict = {}
    for p in players.values():
        if p.team_id:
            team_players.setdefault(p.team_id, []).append(p)

    # Одна причина на пользователя: подписка на игрока важнее подписки на команду.
    reason_by_user: dict = {}
    for f in Follow.objects.filter(player_id__in=players.keys()).select_related('player'):
        reason_by_user.setdefault(f.user_id, f"⭐ {f.player.full_name} — в сборной тура")
    for f in Follow.objects.filter(team_id__in=team_players.keys()).select_related('team'):
        in_xi = team_players[f.team_id]
        reason = f"⭐ {f.team.name}: {len(in_xi)} в сборной тура" if len(in_xi) > 1 else f"⭐ {in_xi[0].full_name} — в сборной тура"
        reason_by_user.setdefault(f.user_id, reason)

    action_url = reverse('round_squad:round', args=[round_xi.season_id, round_xi.tour])
    already = set(
        Notification.objects.filter(
            notification_type='round_results', action_url=action_url, user_id__in=reason_by_user.keys(),
        ).values_list('user_id', flat=True)
    )
    reason_by_user = {uid: r for uid, r in reason_by_user.items() if uid not in already}
    if not reason_by_user:
        return 0

    body = f"{round_xi.brand_title} готовы — посмотрите состав."
    Notification.objects.bulk_create([
        Notification(
            user_id=uid, notification_type='round_results', title=title, message=body, action_url=action_url,
        )
        for uid, title in reason_by_user.items()
    ])

    by_title: dict = {}
    for uid, title in reason_by_user.items():
        by_title.setdefault(title, []).append(uid)
    for title, uids in by_title.items():
        _push_fan_out(uids, title, body, action_url, kind='round_results', tag=f'round-{round_xi.id}')

    logger.info("notify_round_xi_followers: %d подписчиков для %s", len(reason_by_user), round_xi.brand_title)
    return len(reason_by_user)


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

        notify_round_xi_followers(round_xi)

        logger.info(
            "send_round_results_notification: поставлено %d пачек (%d пользователей) для %s",
            len(chunks), len(user_ids), round_xi.brand_title,
        )
        return {'queued_chunks': len(chunks), 'total_users': len(user_ids)}
    finally:
        # Снимаем лок сразу, не ждём TTL.
        cache.delete(lock_key)
