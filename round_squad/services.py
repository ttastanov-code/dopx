# round_squad/services.py
"""«Тур недели»: лучший состав 4-3-3, тренер, игрок тура и самый драматичный матч.

Сглаживание по голосам:
    round_score = (v / (v + C)) * raw_avg + (C / (v + C)) * pool_avg
v — голоса за кандидата в туре, pool_avg — среднее по пулу позиции,
C — «виртуальные голоса».
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field

from django.contrib.contenttypes.models import ContentType
from django.db.models import Avg, Count, F, FloatField, Q, Sum
from django.urls import reverse
from django.utils import timezone

from aggregates.models import CoachMatchAggregate, PlayerMatchAggregate
from coaches.models import Coach
from events.models import MatchEvent
from evaluations.models import MatchEvaluation
from lineups.models import MatchLineupPlayer
from matches.models import Match
from players.models import Player
from players.positions import (
    BEST_XI_SLOT_DISPLAY_ORDER,
    BEST_XI_SLOT_LABELS,
    SLOT_PROCESSING_ORDER,
    resolve_lineup_codes,
)
from round_squad.models import RoundBestXI, RoundBestXISlot, RoundPositionRanking

logger = logging.getLogger(__name__)

# «Виртуальные голоса» сглаживания.
ROUND_VOTE_SHRINKAGE_C = 6.0

# Минимум голосов за кандидата в туре.
ROUND_MIN_VOTES_FOR_CANDIDATE = 3

# Порог «данных достаточно» для индикатора доверия на карточке.
ROUND_CONFIDENT_VOTES_THRESHOLD = 10

# Тур считается практически сыгранным при такой доле завершённых матчей
# (терпимо к 1-2 перенесённым матчам).
ROUND_CURRENT_TOUR_MIN_COMPLETION_RATIO = 0.75

# Заметные события для объяснения «почему он в сборной».
NOTABLE_EVENT_TYPES = ("goal", "yellow_card", "red_card", "own_goal", "disallowed_goal")
NOTABLE_EVENT_LABELS = {
    "goal": "гол",
    "yellow_card": "жёлтая карточка",
    "red_card": "красная карточка",
    "own_goal": "автогол",
    "disallowed_goal": "отменённый гол",
}

# Сколько кандидатов пула сохранять в снимок рейтинга позиции.
RANKING_POOL_DEPTH = 10


def resolve_current_tour(season) -> int | None:
    """Какой тур показывать по умолчанию (шапка, /round/, виджет): последний
    зафиксированный, а если таких нет — практически сыгранный.
    """
    tour = (
        RoundBestXI.objects
        .filter(season=season, is_final=True)
        .order_by('-tour')
        .values_list('tour', flat=True)
        .first()
    )
    if tour is not None:
        return tour
    return resolve_practically_closed_tour(season)


def resolve_practically_closed_tour(season) -> int | None:
    """Последний тур с долей завершённых матчей не ниже ROUND_CURRENT_TOUR_MIN_COMPLETION_RATIO."""
    tour_rows = (
        Match.objects.filter(season=season, tour__isnull=False)
        .values('tour')
        .annotate(total=Count('id'), finished=Count('id', filter=Q(status='finished')))
        .order_by('-tour')
    )
    for row in tour_rows:
        total = row['total']
        if total > 0 and (row['finished'] / total) >= ROUND_CURRENT_TOUR_MIN_COMPLETION_RATIO:
            return row['tour']
    return None


@dataclass
class RoundCandidate:
    """Кандидат тура."""
    content_type_id: int
    object_id: str
    name: str
    team_name: str
    photo_url: str
    profile_url: str
    raw_avg: float
    votes: int
    position_code: str = field(default='')


def _round_bayes_score(raw_avg: float, votes: int, pool_avg: float, c: float = ROUND_VOTE_SHRINKAGE_C) -> float:
    if votes <= 0:
        return pool_avg
    weight = votes / (votes + c)
    return weight * raw_avg + (1 - weight) * pool_avg


def _rank_round_pool(candidates: list[RoundCandidate]) -> list[tuple[RoundCandidate, float]]:
    """Ранжирование пула со сглаживанием по голосам."""
    eligible = [c for c in candidates if c.votes >= ROUND_MIN_VOTES_FOR_CANDIDATE]
    if not eligible:
        return []
    total_votes = sum(c.votes for c in eligible)
    pool_avg = (
        sum(c.raw_avg * c.votes for c in eligible) / total_votes
        if total_votes else 0.0
    )
    scored = [(c, round(_round_bayes_score(c.raw_avg, c.votes, pool_avg), 2)) for c in eligible]
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored


def _round_is_complete(season, tour: int) -> bool:
    """Тур закрыт, когда у всех его матчей закрыто голосование."""
    now = timezone.now()
    matches = Match.objects.filter(season=season, tour=tour)
    return matches.exists() and not matches.filter(voting_open_until__gte=now).exists()


def _build_round_player_data(season, tour: int):
    """Возвращает (player_stats, pool_by_code): плоский список для «игрока тура»
    и кандидатов по кодам позиций для заполнения слотов.
    """
    stats_rows = (
        PlayerMatchAggregate.objects
        .filter(match__season=season, match__tour=tour)
        .values("player_id")
        .annotate(raw_avg=Avg("performance_score"), votes=Sum("total_votes"))
    )
    # Позицию берём только из строк, где игрок реально выходил на поле.
    lineup_rows = (
        MatchLineupPlayer.objects
        .filter(lineup__match__season=season, lineup__match__tour=tour)
        .exclude(position="")
        .filter(Q(is_starting=True) | Q(minute_in__isnull=False))
        .values_list("player_id", "position", "field_position", "lineup__team__name")
    )
    # codes — список кодов позиции.
    position_and_team: dict[str, tuple[list[str], str]] = {}
    for player_id, position, field_position, team_name in lineup_rows:
        pid = str(player_id)
        if pid not in position_and_team:
            codes = resolve_lineup_codes(position, field_position)
            position_and_team[pid] = (codes, team_name or '')

    players = {str(p.id): p for p in Player.objects.filter(is_active=True).select_related("team")}

    player_stats: dict[str, RoundCandidate] = {}
    pool_by_code: dict[str, list[RoundCandidate]] = defaultdict(list)
    for row in stats_rows:
        pid = str(row["player_id"])
        player = players.get(pid)
        if not player:
            continue
        codes, team_name = position_and_team.get(pid, ([], ''))
        team_name = team_name or (player.team.name if player.team else "")
        candidate = RoundCandidate(
            content_type_id=ContentType.objects.get_for_model(Player).id,
            object_id=pid,
            name=player.full_name,
            team_name=team_name,
            photo_url=player.photo.url if player.photo else "",
            profile_url=reverse("players:detail", args=[player.id]),
            raw_avg=row["raw_avg"] or 0.0,
            votes=row["votes"] or 0,
            position_code=codes[0] if codes else '',
        )
        player_stats[pid] = candidate
        # Регистрируем кандидата под всеми кодами.
        for code in codes:
            pool_by_code[code].append(candidate)

    return player_stats, pool_by_code


def _build_round_coach_pool(season, tour: int) -> list[RoundCandidate]:
    coach_ct = ContentType.objects.get_for_model(Coach)
    rows = (
        CoachMatchAggregate.objects
        .filter(match__season=season, match__tour=tour)
        .values(
            "coach_id", "avg_tactics", "avg_substitutions", "avg_management", "avg_impact", "total_votes",
            "match__home_coach_id", "match__home_team__name", "match__away_team__name",
        )
    )
    agg: dict[str, dict] = defaultdict(lambda: {"votes": 0, "sum_score": 0.0, "team_name": ""})
    for row in rows:
        cid = str(row["coach_id"])
        match_score = (
            (row["avg_tactics"] or 0.0) + (row["avg_substitutions"] or 0.0)
            + (row["avg_management"] or 0.0) + (row["avg_impact"] or 0.0)
        ) / 4
        votes = row["total_votes"] or 0
        bucket = agg[cid]
        bucket["votes"] += votes
        bucket["sum_score"] += match_score * votes
        is_home = row["coach_id"] == row["match__home_coach_id"]
        bucket["team_name"] = row["match__home_team__name"] if is_home else row["match__away_team__name"]

    coaches = {str(c.id): c for c in Coach.objects.filter(is_active=True)}
    pool = []
    for cid, bucket in agg.items():
        coach = coaches.get(cid)
        if not coach or bucket["votes"] == 0:
            continue
        pool.append(RoundCandidate(
            content_type_id=coach_ct.id,
            object_id=cid,
            name=coach.full_name,
            team_name=bucket["team_name"] or (coach.team.name if coach.team else ""),
            photo_url=coach.photo.url if coach.photo else "",
            profile_url=reverse("coaches:detail", args=[coach.id]),
            raw_avg=bucket["sum_score"] / bucket["votes"],
            votes=bucket["votes"],
        ))
    return pool


def _find_most_dramatic_match(season, tour: int):
    """Самый драматичный матч тура: (match, drama_score, votes) по среднему
    entertainment * tension, с минимумом голосов.
    """
    best = (
        MatchEvaluation.objects
        .filter(match__season=season, match__tour=tour)
        .values("match_id")
        .annotate(
            votes=Count("id"),
            drama_avg=Avg(F("entertainment") * F("tension"), output_field=FloatField()),
        )
        .filter(votes__gte=ROUND_MIN_VOTES_FOR_CANDIDATE)
        .order_by("-drama_avg")
        .first()
    )
    if not best:
        return None, None, None
    match = Match.objects.select_related("home_team", "away_team").filter(pk=best["match_id"]).first()
    return match, best["drama_avg"], best["votes"]


def _describe_nearest_competitor_round(score: float, runner_up: tuple[RoundCandidate, float] | None) -> str:
    """Ближайший конкурент в объяснении."""
    if runner_up is None:
        return ""
    competitor, competitor_score = runner_up
    gap = round(score - competitor_score, 2)
    if gap <= 0:
        return ""
    return f"Обошёл ближайшего конкурента: {competitor.name} ({competitor_score:.2f}), разница {gap:.2f}."


def _describe_round_rank_change(rank_change: str, rank_change_delta: int | None) -> str:
    """Изменение позиции относительно прошлого зафиксированного тура."""
    if rank_change == RoundBestXISlot.RANK_CHANGE_NEW:
        return ""  # не играл в прошлом туре — не показываем
    if rank_change == RoundBestXISlot.RANK_CHANGE_UP and rank_change_delta:
        matches_word = "место" if rank_change_delta == 1 else "места"
        return f"Поднялся на {rank_change_delta} {matches_word} по сравнению с прошлым туром."
    return ""


def _build_round_explanation(
    label: str, candidate: RoundCandidate, score: float, is_confident: bool,
    rank_change: str = RoundBestXISlot.RANK_CHANGE_NEW, rank_change_delta: int | None = None,
    runner_up: tuple[RoundCandidate, float] | None = None,
) -> str:
    """Объяснение для слота — список фактов, по одному на строку."""
    lines = [
        f"Рейтинг {score:.2f} на позиции «{label}» в этом туре: среднее по "
        f"{candidate.votes} голосам с поправкой на их число."
    ]
    if is_confident:
        lines.append("Голосов достаточно, чтобы доверять этому месту.")
    else:
        lines.append("Голосов пока немного: оценка может быть неточной.")

    rank_change_text = _describe_round_rank_change(rank_change, rank_change_delta)
    if rank_change_text:
        lines.append(rank_change_text)

    competitor_text = _describe_nearest_competitor_round(score, runner_up)
    if competitor_text:
        lines.append(competitor_text)

    return "\n".join(lines)


def _describe_notable_events_in_round(player_id: str, season, tour: int) -> str:
    """Заметные события игрока в его матче тура."""
    match_id = (
        PlayerMatchAggregate.objects
        .filter(player_id=player_id, match__season=season, match__tour=tour)
        .values_list("match_id", flat=True)
        .first()
    )
    if not match_id:
        return ""
    events = list(
        MatchEvent.objects
        .filter(player_id=player_id, match_id=match_id, event_type__in=NOTABLE_EVENT_TYPES)
        .order_by("minute")
    )
    if not events:
        return ""
    ev_text = ", ".join(
        f"{NOTABLE_EVENT_LABELS.get(e.event_type, e.event_type)} {e.display_minute}'" for e in events
    )
    return f"Отличился: {ev_text}."


def _store_round_ranking_batch(
    buffer: list[RoundPositionRanking],
    round_best_xi: RoundBestXI,
    slot_code: str,
    ranked: list[tuple[RoundCandidate, float]],
) -> None:
    """Снимок рейтинга позиции (delete + bulk_create на каждый пересчёт)."""
    for rank, (candidate, score) in enumerate(ranked[:RANKING_POOL_DEPTH], start=1):
        buffer.append(RoundPositionRanking(
            round_best_xi=round_best_xi,
            slot_code=slot_code,
            content_type_id=candidate.content_type_id,
            object_id=candidate.object_id,
            rank=rank,
            round_score=score,
            votes_count=candidate.votes,
        ))


def _apply_round_slot(
    round_best_xi: RoundBestXI, slot_code: str, candidate: RoundCandidate | None, score: float | None,
    season=None, tour: int | None = None,
    previous_ranks: dict[tuple[str, int, str], int] | None = None,
    runner_up: tuple[RoundCandidate, float] | None = None,
) -> None:
    order = BEST_XI_SLOT_DISPLAY_ORDER.get(slot_code, 99)
    label = BEST_XI_SLOT_LABELS.get(slot_code, slot_code)

    if candidate is None:
        RoundBestXISlot.objects.update_or_create(
            round_best_xi=round_best_xi, slot_code=slot_code,
            defaults=dict(
                order=order, content_type=None, object_id=None,
                occupant_name="", occupant_team_name="", occupant_photo_url="", occupant_profile_url="",
                round_score=None, votes_count=0, is_confident=False,
                rank_change=RoundBestXISlot.RANK_CHANGE_NEW, rank_change_delta=None,
                explanation="Пока недостаточно голосов на этой позиции в этом туре.",
            ),
        )
        return

    previous_ranks = previous_ranks or {}
    prev_rank = previous_ranks.get((slot_code, candidate.content_type_id, candidate.object_id))
    if prev_rank is None:
        rank_change, delta = RoundBestXISlot.RANK_CHANGE_NEW, None
    elif prev_rank == 1:
        rank_change, delta = RoundBestXISlot.RANK_CHANGE_SAME, None
    else:
        rank_change, delta = RoundBestXISlot.RANK_CHANGE_UP, prev_rank - 1

    is_confident = candidate.votes >= ROUND_CONFIDENT_VOTES_THRESHOLD
    explanation = _build_round_explanation(label, candidate, score, is_confident, rank_change, delta, runner_up)
    if slot_code != "COACH" and season is not None and tour is not None:
        events_text = _describe_notable_events_in_round(candidate.object_id, season, tour)
        if events_text:
            explanation = f"{explanation}\n{events_text}"
    RoundBestXISlot.objects.update_or_create(
        round_best_xi=round_best_xi, slot_code=slot_code,
        defaults=dict(
            order=order,
            content_type_id=candidate.content_type_id, object_id=candidate.object_id,
            occupant_name=candidate.name, occupant_team_name=candidate.team_name,
            occupant_photo_url=candidate.photo_url, occupant_profile_url=candidate.profile_url,
            round_score=score, votes_count=candidate.votes, is_confident=is_confident,
            rank_change=rank_change, rank_change_delta=delta,
            explanation=explanation,
        ),
    )


def recompute_round(season, tour: int, *, force: bool = False) -> RoundBestXI:
    """Пересчёт тура. Идемпотентна.

    force=True пересчитывает и уже зафиксированный тур (для recompute_all_closed_rounds),
    но не меняет finalized_at и не рассылает письмо об итогах повторно.
    """
    round_best_xi, _created = RoundBestXI.objects.get_or_create(season=season, tour=tour)
    was_final_before = round_best_xi.is_final
    if was_final_before and not force:
        logger.info("Тур %s сезона %s уже зафиксирован — пересчёт пропущен", tour, season)
        return round_best_xi

    now = timezone.now()
    player_stats, pool_by_code = _build_round_player_data(season, tour)
    coach_pool = _build_round_coach_pool(season, tour)

    # Снимок прошлого тура для «изменения позиции».
    previous_round = (
        RoundBestXI.objects.filter(season=season, tour__lt=tour).order_by('-tour').first()
    )
    previous_ranks: dict[tuple[str, int, str], int] = {}
    if previous_round is not None:
        for row in RoundPositionRanking.objects.filter(
            round_best_xi=previous_round
        ).values("slot_code", "content_type_id", "object_id", "rank"):
            previous_ranks[(row["slot_code"], row["content_type_id"], str(row["object_id"]))] = row["rank"]

    # Снимок этого тура перезаписывается полностью.
    RoundPositionRanking.objects.filter(round_best_xi=round_best_xi).delete()
    ranking_buffer: list[RoundPositionRanking] = []

    # ---- 11 слотов 4-3-3: жадное заполнение по SLOT_PROCESSING_ORDER ----
    assigned: set[str] = set()
    for slot_code, raw_codes in SLOT_PROCESSING_ORDER:
        candidates: list[RoundCandidate] = []
        seen: set[str] = set()
        for code in raw_codes:
            for cand in pool_by_code.get(code, []):
                if cand.object_id in assigned or cand.object_id in seen:
                    continue
                seen.add(cand.object_id)
                candidates.append(cand)

        ranked = _rank_round_pool(candidates)
        _store_round_ranking_batch(ranking_buffer, round_best_xi, slot_code, ranked)
        if ranked:
            top_candidate, top_score = ranked[0]
            assigned.add(top_candidate.object_id)
            runner_up = ranked[1] if len(ranked) > 1 else None
            _apply_round_slot(round_best_xi, slot_code, top_candidate, top_score, season, tour, previous_ranks, runner_up)
        else:
            _apply_round_slot(round_best_xi, slot_code, None, None, season, tour, previous_ranks, None)

    # ---- Тренер тура ----
    coach_ranked = _rank_round_pool(coach_pool)
    _store_round_ranking_batch(ranking_buffer, round_best_xi, "COACH", coach_ranked)
    if coach_ranked:
        top_coach, coach_score = coach_ranked[0]
        coach_runner_up = coach_ranked[1] if len(coach_ranked) > 1 else None
        _apply_round_slot(round_best_xi, "COACH", top_coach, coach_score, season, tour, previous_ranks, coach_runner_up)
    else:
        _apply_round_slot(round_best_xi, "COACH", None, None, season, tour, previous_ranks, None)

    RoundPositionRanking.objects.bulk_create(ranking_buffer, batch_size=200)

    # ---- Игрок тура (по всему пулу) ----
    flat_ranked = _rank_round_pool(list(player_stats.values()))
    if flat_ranked:
        top_player, player_score = flat_ranked[0]
        is_confident = top_player.votes >= ROUND_CONFIDENT_VOTES_THRESHOLD
        round_best_xi.player_of_round_content_type_id = top_player.content_type_id
        round_best_xi.player_of_round_object_id = top_player.object_id
        round_best_xi.player_of_round_name = top_player.name
        round_best_xi.player_of_round_team_name = top_player.team_name
        round_best_xi.player_of_round_photo_url = top_player.photo_url
        round_best_xi.player_of_round_profile_url = top_player.profile_url
        round_best_xi.player_of_round_score = player_score
        round_best_xi.player_of_round_votes = top_player.votes
        # Объяснение — список строк.
        player_of_round_lines = [
            f"Лучший результат тура среди всех позиций: {player_score:.2f} "
            f"по {top_player.votes} голосам."
        ]
        player_of_round_lines.append(
            "Голосов достаточно, чтобы доверять этому выбору." if is_confident
            else "Голосов пока немного: выбор может измениться."
        )
        # Ближайший конкурент игрока тура — второй в общем рейтинге.
        flat_runner_up = flat_ranked[1] if len(flat_ranked) > 1 else None
        competitor_text = _describe_nearest_competitor_round(player_score, flat_runner_up)
        if competitor_text:
            player_of_round_lines.append(competitor_text)
        events_text = _describe_notable_events_in_round(top_player.object_id, season, tour)
        if events_text:
            player_of_round_lines.append(events_text)
        round_best_xi.player_of_round_explanation = "\n".join(player_of_round_lines)
    else:
        round_best_xi.player_of_round_content_type = None
        round_best_xi.player_of_round_object_id = None
        round_best_xi.player_of_round_name = ""
        round_best_xi.player_of_round_team_name = ""
        round_best_xi.player_of_round_photo_url = ""
        round_best_xi.player_of_round_profile_url = ""
        round_best_xi.player_of_round_score = None
        round_best_xi.player_of_round_votes = 0
        round_best_xi.player_of_round_explanation = ""

    # ---- Самый драматичный матч тура ----
    dramatic_match, drama_score, drama_votes = _find_most_dramatic_match(season, tour)
    round_best_xi.most_dramatic_match = dramatic_match
    round_best_xi.most_dramatic_match_score = drama_score
    if dramatic_match:
        round_best_xi.most_dramatic_match_explanation = (
            f"{dramatic_match.home_team.name} {dramatic_match.home_score}:{dramatic_match.away_score} "
            f"{dramatic_match.away_team.name}: самый высокий индекс зрелищности тура "
            f"({drama_score:.1f}, по {drama_votes} оценкам матча)."
        )
    else:
        round_best_xi.most_dramatic_match_explanation = ""

    # ---- Финализация: голосование по всем матчам закрыто — фиксируем тур ----
    just_finalized = False
    if _round_is_complete(season, tour):
        # «Только что закрылся» — по состоянию до вызова, а не по входу в этот if.
        just_finalized = not was_final_before
        round_best_xi.is_final = True
        if just_finalized:
            round_best_xi.finalized_at = now
        try:
            from core.services.share_cards import build_round_squad_share_card

            round_best_xi.share_card_path = build_round_squad_share_card(
                season_year=season.year,
                tour=tour,
                player_of_round_name=round_best_xi.player_of_round_name or "—",
                player_of_round_score=round_best_xi.player_of_round_score,
                dramatic_match_label=(
                    f"{dramatic_match.home_team.name} {dramatic_match.home_score}:{dramatic_match.away_score} "
                    f"{dramatic_match.away_team.name}" if dramatic_match else ""
                ),
            )
        except Exception:
            # Карточка не критична — тур фиксируется и без неё.
            logger.exception("Тур %s сезона %s: не удалось собрать share-карточку", tour, season)

    round_best_xi.last_computed_at = now
    round_best_xi.save()

    if just_finalized:
        # Ставим после save(), чтобы воркер прочитал уже сохранённые данные.
        try:
            from round_squad.tasks import send_round_results_notification

            send_round_results_notification.delay(str(round_best_xi.id))
        except Exception:
            # Рассылка не критична — тур остаётся зафиксированным.
            logger.exception("Тур %s сезона %s: не удалось поставить в очередь рассылку итогов", tour, season)

    logger.info(
        "Тур %s сезона %s пересчитан (is_final=%s), игроков в составе: %d",
        tour, season, round_best_xi.is_final, len(assigned),
    )
    return round_best_xi


def recompute_all_closed_rounds() -> int:
    """Пересчитывает все зафиксированные туры (force=True) — после исправления
    данных задним числом. Письма повторно не рассылаются.
    Возвращает число пересчитанных туров.
    """
    closed = list(
        RoundBestXI.objects.filter(is_final=True).select_related('season').order_by('season_id', 'tour')
    )
    for round_xi in closed:
        recompute_round(round_xi.season, round_xi.tour, force=True)

    logger.info("recompute_all_closed_rounds: пересчитано закрытых туров: %d", len(closed))
    return len(closed)
