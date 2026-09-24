# season_squad/services.py
"""«Живая сборная сезона»: 4-3-3 + тренер + судья по оценкам пользователей.

Байесовское сглаживание (как weighted rating у IMDB):
    season_score = (m / (m + C)) * raw_avg + (C / (m + C)) * pool_avg
m — оценённых матчей у кандидата, pool_avg — среднее по его пулу (взвешенное по матчам).

Слоты заполняются жадно в порядке SLOT_PROCESSING_ORDER (от узких амплуа к широким),
назначенный игрок выбывает из остальных пулов.
"""
from __future__ import annotations

import logging
from collections import Counter, defaultdict
from dataclasses import dataclass

from django.contrib.contenttypes.models import ContentType
from django.db.models import Avg, Count, Q, Sum
from django.urls import reverse
from django.utils import timezone

from aggregates.models import CoachMatchAggregate, PlayerMatchAggregate, RefereeMatchAggregate
from aggregates.services import CONFIDENT_VOTES_THRESHOLD
from coaches.models import Coach
from events.models import MatchEvent
from lineups.models import MatchLineupPlayer
from matches.models import Match
from players.models import Player
from players.positions import (
    BEST_XI_SLOT_DISPLAY_ORDER,
    BEST_XI_SLOT_LABELS,
    SLOT_PROCESSING_ORDER,
    resolve_lineup_codes,
)
from referees.models import Referee
from season_squad.models import SeasonBestXI, SeasonBestXISlot, SeasonPositionRanking

logger = logging.getLogger(__name__)

# C для байесовского сглаживания — «виртуальные матчи».
SHRINKAGE_C = 6.0

# Минимум оценённых матчей для участия в подборе.
MIN_MATCHES_FOR_CANDIDATE = 2

# Сколько последних батчей ранжирования хранить.
RANKING_BATCHES_TO_KEEP = 5

# Сколько кандидатов пула сохранять в снимок.
RANKING_POOL_DEPTH = 10

# Заметные события для блока «Почему он в сборной?».
NOTABLE_EVENT_TYPES = ("goal", "yellow_card", "red_card", "own_goal", "disallowed_goal")
NOTABLE_EVENT_LABELS = {
    "goal": "гол",
    "yellow_card": "жёлтая карточка",
    "red_card": "красная карточка",
    "own_goal": "автогол",
    "disallowed_goal": "отменённый гол",
}
TOP_MATCHES_FOR_EXPLANATION = 2


@dataclass
class Candidate:
    """Кандидат (игрок/тренер/судья) в общем виде для _rank_pool."""
    content_type_id: int
    object_id: str
    name: str
    team_name: str
    photo_url: str
    profile_url: str
    raw_avg: float
    matches: int
    votes: int


def _bayes_score(raw_avg: float, matches: int, pool_avg: float, c: float = SHRINKAGE_C) -> float:
    if matches <= 0:
        return pool_avg
    weight = matches / (matches + c)
    return weight * raw_avg + (1 - weight) * pool_avg


def _rank_pool(candidates: list[Candidate]) -> list[tuple[Candidate, float]]:
    """Байес-скор для каждого кандидата пула, по убыванию."""
    eligible = [c for c in candidates if c.matches >= MIN_MATCHES_FOR_CANDIDATE]
    if not eligible:
        return []
    total_matches = sum(c.matches for c in eligible)
    pool_avg = (
        sum(c.raw_avg * c.matches for c in eligible) / total_matches
        if total_matches else 0.0
    )
    scored = [(c, round(_bayes_score(c.raw_avg, c.matches, pool_avg), 2)) for c in eligible]
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored


def _player_season_position(season) -> dict[str, str]:
    """player_id -> самый частый код позиции в сезоне (с учётом стороны, напр. 'D:L').
    Считаем только по матчам, где игрок реально выходил на поле.
    """
    rows = (
        MatchLineupPlayer.objects
        .filter(lineup__match__season=season)
        .exclude(position="")
        .filter(Q(is_starting=True) | Q(minute_in__isnull=False))
        .values_list("player_id", "position", "field_position")
    )
    counters: dict[str, Counter] = defaultdict(Counter)
    for player_id, position, field_position in rows:
        codes = resolve_lineup_codes(position, field_position)
        if not codes:
            continue
        counters[str(player_id)][codes[0]] += 1
    return {pid: counter.most_common(1)[0][0] for pid, counter in counters.items() if counter}


def _player_season_team_name(season) -> dict[str, str]:
    """player_id -> клуб из последнего матча сезона (а не текущий player.team)."""
    rows = (
        MatchLineupPlayer.objects
        .filter(lineup__match__season=season)
        .order_by('player_id', '-lineup__match__start_time')
        .values_list('player_id', 'lineup__team__name')
    )
    latest: dict[str, str] = {}
    for player_id, team_name in rows:
        pid = str(player_id)
        if pid not in latest:
            latest[pid] = team_name or ''
    return latest


def _coach_season_team_name(season) -> dict[str, str]:
    """То же для тренеров — через Match.home_coach/away_coach."""
    rows = (
        Match.objects
        .filter(season=season)
        .exclude(home_coach__isnull=True, away_coach__isnull=True)
        .order_by('-start_time')
        .values_list('home_coach_id', 'home_team__name', 'away_coach_id', 'away_team__name')
    )
    latest: dict[str, str] = {}
    for home_coach_id, home_team_name, away_coach_id, away_team_name in rows:
        if home_coach_id is not None:
            key = str(home_coach_id)
            if key not in latest:
                latest[key] = home_team_name or ''
        if away_coach_id is not None:
            key = str(away_coach_id)
            if key not in latest:
                latest[key] = away_team_name or ''
    return latest


def _build_player_pool_by_code(season, player_ct: ContentType) -> dict[str, list[Candidate]]:
    position_by_player = _player_season_position(season)
    team_name_by_player = _player_season_team_name(season)
    stats = (
        PlayerMatchAggregate.objects
        .filter(match__season=season)
        .values("player_id")
        .annotate(raw_avg=Avg("performance_score"), matches=Count("id"), votes=Sum("total_votes"))
    )
    players = {str(p.id): p for p in Player.objects.filter(is_active=True).select_related("team")}

    pool: dict[str, list[Candidate]] = defaultdict(list)
    for row in stats:
        pid = str(row["player_id"])
        player = players.get(pid)
        if not player:
            continue
        code = position_by_player.get(pid)
        if not code:
            continue
        pool[code].append(Candidate(
            content_type_id=player_ct.id,
            object_id=pid,
            name=player.full_name,
            # Клуб за этот сезон; player.team — запасной вариант.
            team_name=team_name_by_player.get(pid) or (player.team.name if player.team else ""),
            photo_url=player.photo.url if player.photo else "",
            profile_url=reverse("players:detail", args=[player.id]),
            raw_avg=row["raw_avg"] or 0.0,
            matches=row["matches"] or 0,
            votes=row["votes"] or 0,
        ))
    return pool


def _build_coach_pool(season, coach_ct: ContentType) -> list[Candidate]:
    team_name_by_coach = _coach_season_team_name(season)
    stats = (
        CoachMatchAggregate.objects
        .filter(match__season=season)
        .values("coach_id")
        .annotate(
            avg_t=Avg("avg_tactics"), avg_s=Avg("avg_substitutions"),
            avg_m=Avg("avg_management"), avg_i=Avg("avg_impact"),
            matches=Count("id"), votes=Sum("total_votes"),
        )
    )
    coaches = {str(c.id): c for c in Coach.objects.filter(is_active=True).select_related("team")}

    pool = []
    for row in stats:
        cid = str(row["coach_id"])
        coach = coaches.get(cid)
        if not coach:
            continue
        # Среднее по матчам — пересчитывать из сырых оценок не нужно.
        raw_avg = (
            (row["avg_t"] or 0.0) + (row["avg_s"] or 0.0)
            + (row["avg_m"] or 0.0) + (row["avg_i"] or 0.0)
        ) / 4
        pool.append(Candidate(
            content_type_id=coach_ct.id,
            object_id=cid,
            name=coach.full_name,
            team_name=team_name_by_coach.get(cid) or (coach.team.name if coach.team else ""),
            photo_url=coach.photo.url if coach.photo else "",
            profile_url=reverse("coaches:detail", args=[coach.id]),
            raw_avg=raw_avg,
            matches=row["matches"] or 0,
            votes=row["votes"] or 0,
        ))
    return pool


def _build_referee_pool(season, referee_ct: ContentType) -> list[Candidate]:
    """Лучший судья — по готовому RefereeMatchAggregate.performance_score,
    усреднение по матчам сезона.
    """
    match_level = (
        RefereeMatchAggregate.objects
        .filter(match__season=season)
        .values("referee_id")
        .annotate(
            avg_performance=Avg("performance_score"),
            matches=Count("id"),
            votes=Sum("total_votes"),
        )
    )

    referees = {str(r.id): r for r in Referee.objects.filter(is_active=True)}
    pool = []
    for row in match_level:
        rid = str(row["referee_id"])
        referee = referees.get(rid)
        if not referee or not row["matches"]:
            continue
        pool.append(Candidate(
            content_type_id=referee_ct.id,
            object_id=rid,
            name=referee.full_name,
            team_name="",
            photo_url=referee.photo.url if referee.photo else "",
            profile_url=reverse("referees:detail", args=[referee.id]),
            raw_avg=row["avg_performance"] or 0.0,
            matches=row["matches"],
            votes=row["votes"] or 0,
        ))
    return pool


def _describe_nearest_competitor(score: float, runner_up: tuple[Candidate, float] | None) -> str:
    """Фраза «обошёл ближайшего конкурента на N».
    Не показываем без runner_up или при gap <= 0.
    """
    if runner_up is None:
        return ""
    competitor, competitor_score = runner_up
    gap = round(score - competitor_score, 2)
    if gap <= 0:
        return ""
    return f"Обошёл ближайшего конкурента: {competitor.name} ({competitor_score:.2f}), разница {gap:.2f}."


def _build_explanation(
    slot_code: str,
    candidate: Candidate,
    score: float,
    is_confident: bool,
    rank_change: str,
    rank_change_delta: int | None,
    runner_up: tuple[Candidate, float] | None = None,
) -> str:
    """Текст тултипа карточки: уверенность, rank_change и т.д.
    Каждый факт — отдельная строка.
    """
    label = BEST_XI_SLOT_LABELS.get(slot_code, slot_code)
    lines = [
        f"Рейтинг {score:.2f} на позиции «{label}»: среднее за сезон "
        f"({candidate.matches} матчей, {candidate.votes} голосов)."
    ]

    if is_confident:
        lines.append("Голосов достаточно, чтобы доверять этому месту в составе.")
    else:
        lines.append("Голосов пока немного: место может измениться, когда их станет больше.")

    if rank_change == SeasonBestXISlot.RANK_CHANGE_NEW:
        lines.append("Занял место в составе по итогам последнего пересчёта.")
    elif rank_change == SeasonBestXISlot.RANK_CHANGE_UP and rank_change_delta:
        matches_word = "место" if rank_change_delta == 1 else "места"
        lines.append(f"Поднялся на {rank_change_delta} {matches_word} с прошлого пересчёта.")

    competitor_text = _describe_nearest_competitor(score, runner_up)
    if competitor_text:
        lines.append(competitor_text)

    return "\n".join(lines)


def _describe_top_matches(player_id: str, season, limit: int = TOP_MATCHES_FOR_EXPLANATION) -> str:
    """Лучшие матчи игрока в сезоне для тултипа (дата, соперник, события)."""
    top = list(
        PlayerMatchAggregate.objects
        .filter(player_id=player_id, match__season=season)
        .select_related("match", "match__home_team", "match__away_team")
        .order_by("-performance_score")[:limit]
    )
    if not top:
        return ""

    match_ids = [pma.match_id for pma in top]
    own_team_by_match: dict = {
        row["lineup__match_id"]: row["lineup__team_id"]
        for row in MatchLineupPlayer.objects.filter(
            player_id=player_id, lineup__match_id__in=match_ids
        ).values("lineup__match_id", "lineup__team_id")
    }
    events_by_match: dict[str, list] = defaultdict(list)
    for event in (
        MatchEvent.objects
        .filter(player_id=player_id, match_id__in=match_ids, event_type__in=NOTABLE_EVENT_TYPES)
        .order_by("minute")
    ):
        events_by_match[event.match_id].append(event)

    # Каждый матч — своя строка с маркером «·».
    pieces = []
    for pma in top:
        match = pma.match
        own_team_id = own_team_by_match.get(match.id)
        opponent = match.away_team if own_team_id == match.home_team_id else match.home_team
        piece = f"· {pma.performance_score:.1f} ({match.start_time:%d.%m}, {opponent.name if opponent else '?'}"
        events = events_by_match.get(match.id, [])
        if events:
            ev_text = ", ".join(
                f"{NOTABLE_EVENT_LABELS.get(e.event_type, e.event_type)} {e.display_minute}'"
                for e in events
            )
            piece += f", {ev_text}"
        piece += ")"
        pieces.append(piece)
    return "Лучшие матчи:\n" + "\n".join(pieces)


def _store_ranking_batch(
    buffer: list[SeasonPositionRanking],
    best_xi: SeasonBestXI,
    slot_code: str,
    ranked: list[tuple[Candidate, float]],
    computed_at,
) -> None:
    for rank, (candidate, score) in enumerate(ranked[:RANKING_POOL_DEPTH], start=1):
        buffer.append(SeasonPositionRanking(
            best_xi=best_xi,
            slot_code=slot_code,
            content_type_id=candidate.content_type_id,
            object_id=candidate.object_id,
            rank=rank,
            season_score=score,
            matches_count=candidate.matches,
            votes_count=candidate.votes,
            computed_at=computed_at,
        ))


def _apply_slot(
    best_xi: SeasonBestXI,
    slot_code: str,
    candidate: Candidate | None,
    score: float | None,
    previous_ranks: dict[tuple[str, int, str], int],
    season,
    runner_up: tuple[Candidate, float] | None = None,
) -> None:
    """Записывает карточку слота.
    rank_change: SAME / UP (delta=N-1) / NEW. DOWN для occupant недостижим.
    """
    order = BEST_XI_SLOT_DISPLAY_ORDER.get(slot_code, 99)

    if candidate is None:
        SeasonBestXISlot.objects.update_or_create(
            best_xi=best_xi, slot_code=slot_code,
            defaults=dict(
                order=order,
                content_type=None, object_id=None,
                occupant_name="", occupant_team_name="",
                occupant_photo_url="", occupant_profile_url="",
                season_score=None, matches_count=0, votes_count=0, is_confident=False,
                rank_change=SeasonBestXISlot.RANK_CHANGE_NEW, rank_change_delta=None,
                explanation="Пока недостаточно оценённых матчей на этой позиции: "
                            "покажем, как только наберётся минимум данных.",
            ),
        )
        return

    prev_rank = previous_ranks.get((slot_code, candidate.content_type_id, candidate.object_id))
    if prev_rank is None:
        rank_change, delta = SeasonBestXISlot.RANK_CHANGE_NEW, None
    elif prev_rank == 1:
        rank_change, delta = SeasonBestXISlot.RANK_CHANGE_SAME, None
    else:
        rank_change, delta = SeasonBestXISlot.RANK_CHANGE_UP, prev_rank - 1

    is_confident = candidate.votes >= CONFIDENT_VOTES_THRESHOLD
    explanation = _build_explanation(slot_code, candidate, score, is_confident, rank_change, delta, runner_up)
    if slot_code not in ("COACH", "REFEREE"):
        # Матчи с событиями — только для игроков.
        top_matches_text = _describe_top_matches(candidate.object_id, season)
        if top_matches_text:
            # Пустая строка-разделитель между блоками тултипа.
            explanation = f"{explanation}\n\n{top_matches_text}"
    SeasonBestXISlot.objects.update_or_create(
        best_xi=best_xi, slot_code=slot_code,
        defaults=dict(
            order=order,
            content_type_id=candidate.content_type_id, object_id=candidate.object_id,
            occupant_name=candidate.name, occupant_team_name=candidate.team_name,
            occupant_photo_url=candidate.photo_url, occupant_profile_url=candidate.profile_url,
            season_score=score, matches_count=candidate.matches, votes_count=candidate.votes,
            is_confident=is_confident,
            rank_change=rank_change, rank_change_delta=delta,
            explanation=explanation,
        ),
    )


def _prune_old_rankings(best_xi: SeasonBestXI, keep_batches: int = RANKING_BATCHES_TO_KEEP) -> None:
    batches = list(
        SeasonPositionRanking.objects
        .filter(best_xi=best_xi)
        .order_by("-computed_at")
        .values_list("computed_at", flat=True)
        .distinct()
    )
    stale = batches[keep_batches:]
    if stale:
        SeasonPositionRanking.objects.filter(best_xi=best_xi, computed_at__in=stale).delete()


def recompute_best_xi(season) -> SeasonBestXI:
    """Пересчёт сборной. Вызывается из Celery Beat и из админки. Идемпотентна."""
    best_xi, _created = SeasonBestXI.objects.get_or_create(season=season)
    if best_xi.is_final:
        logger.info("Сборная сезона %s зафиксирована как итоговая — пересчёт пропущен", season)
        return best_xi

    now = timezone.now()
    player_ct = ContentType.objects.get_for_model(Player)
    coach_ct = ContentType.objects.get_for_model(Coach)
    referee_ct = ContentType.objects.get_for_model(Referee)

    player_pool = _build_player_pool_by_code(season, player_ct)
    coach_pool = _build_coach_pool(season, coach_ct)
    referee_pool = _build_referee_pool(season, referee_ct)

    # Предыдущий батч — берём до записи нового.
    previous_batch_at = (
        SeasonPositionRanking.objects
        .filter(best_xi=best_xi)
        .order_by("-computed_at")
        .values_list("computed_at", flat=True)
        .first()
    )
    previous_ranks: dict[tuple[str, int, str], int] = {}
    if previous_batch_at:
        for row in SeasonPositionRanking.objects.filter(
            best_xi=best_xi, computed_at=previous_batch_at
        ).values("slot_code", "content_type_id", "object_id", "rank"):
            previous_ranks[(row["slot_code"], row["content_type_id"], str(row["object_id"]))] = row["rank"]

    assigned: set[tuple[int, str]] = set()
    ranking_buffer: list[SeasonPositionRanking] = []
    # 4-й элемент — runner_up для _build_explanation.
    slot_results: list[tuple[str, Candidate | None, float | None, tuple[Candidate, float] | None]] = []

    # ---- 11 слотов 4-3-3 — жадное распределение ----
    for slot_code, raw_codes in SLOT_PROCESSING_ORDER:
        candidates: list[Candidate] = []
        seen: set[tuple[int, str]] = set()
        for code in raw_codes:
            for cand in player_pool.get(code, []):
                key = (cand.content_type_id, cand.object_id)
                if key in assigned or key in seen:
                    continue
                seen.add(key)
                candidates.append(cand)

        ranked = _rank_pool(candidates)
        _store_ranking_batch(ranking_buffer, best_xi, slot_code, ranked, now)
        if ranked:
            top_candidate, top_score = ranked[0]
            assigned.add((top_candidate.content_type_id, top_candidate.object_id))
            runner_up = ranked[1] if len(ranked) > 1 else None
            slot_results.append((slot_code, top_candidate, top_score, runner_up))
        else:
            slot_results.append((slot_code, None, None, None))

    # ---- Тренер и судья — отдельные пулы ----
    for slot_code, pool in (("COACH", coach_pool), ("REFEREE", referee_pool)):
        ranked = _rank_pool(pool)
        _store_ranking_batch(ranking_buffer, best_xi, slot_code, ranked, now)
        if ranked:
            top_candidate, top_score = ranked[0]
            runner_up = ranked[1] if len(ranked) > 1 else None
            slot_results.append((slot_code, top_candidate, top_score, runner_up))
        else:
            slot_results.append((slot_code, None, None, None))

    SeasonPositionRanking.objects.bulk_create(ranking_buffer, batch_size=200)

    for slot_code, candidate, score, runner_up in slot_results:
        _apply_slot(best_xi, slot_code, candidate, score, previous_ranks, season, runner_up)

    _prune_old_rankings(best_xi)

    best_xi.last_computed_at = now
    best_xi.save(update_fields=["last_computed_at"])
    # Считаем реально заполненные слоты (тренер/судья могут быть пустыми).
    filled_slots = sum(1 for _, candidate, _, _ in slot_results if candidate is not None)
    logger.info("Живая сборная сезона %s пересчитана: %d слотов заполнено", season, filled_slots)
    return best_xi


def finalize_best_xi(season) -> SeasonBestXI:
    """Фиксирует сборную как итоговую (из админки после сезона). Дальше recompute — no-op."""
    best_xi = SeasonBestXI.objects.get(season=season)
    best_xi.is_final = True
    best_xi.finalized_at = timezone.now()
    best_xi.save(update_fields=["is_final", "finalized_at"])
    return best_xi
