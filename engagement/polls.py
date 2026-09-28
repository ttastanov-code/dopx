# engagement/polls.py
"""Опросы недели: «Спорный момент» (вт) и «Дуэль тура» (ср).

Спорный момент выбирается из событий матчей последнего тура: VAR, пенальти, удаления,
отменённые голы. Индекс спорности = вес типа события + поздняя минута + влияние на счёт
+ дерби + разногласие фанатов по судье + интерес к матчу (число оценок).
Дуэль — два игрока тура с самыми близкими оценками болельщиков (без оценок — по статистике)."""
from __future__ import annotations

import logging
import math
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import Count
from django.utils import timezone

logger = logging.getLogger(__name__)

POLL_DAYS = 7
DUEL_POOL = 8
# Вес типа события: насколько вероятен спор.
EVENT_WEIGHT = {"var_check": 3.0, "disallowed_goal": 3.0, "penalty": 3.0, "red_card": 2.5, "missed_penalty": 2.0}


def last_round_matches():
    """Матчи последнего сыгранного тура основного сезона (или последних 8 дней)."""
    from engagement.season import current_season
    from matches.models import Match

    season = current_season()
    finished = Match.objects.filter(status="finished").select_related("home_team", "away_team", "season")
    if season:
        finished = finished.filter(season=season)
    last = finished.exclude(tour__isnull=True).order_by("-start_time").first()
    if last:
        return list(finished.filter(season=last.season, tour=last.tour).prefetch_related("home_team__rivals"))
    since = timezone.now() - timedelta(days=8)
    return list(finished.filter(start_time__gte=since).prefetch_related("home_team__rivals"))


def _episode_text(event, match) -> tuple[str, str, str, str] | None:
    """(вопрос, вариант A, вариант B, описание) для события; None — событие не спорное."""
    raw = event.extra_data or {}
    info = f"{raw.get('info') or ''} {raw.get('addition') or ''} {event.var_decision or ''}".lower()
    who = event.player.full_name if event.player else (raw.get("player_name") or "")
    team = match.home_team.name if event.team_side == "home" else match.away_team.name
    t = event.event_type
    if t in ("var_check", "disallowed_goal"):
        if "penalty" in info:
            return "VAR назначил пенальти. Верное решение?", "Да, пенальти", "Нет, не было", f"VAR: пенальти в пользу {team}"
        if "card" in info:
            return "VAR вмешался в карточку. Справедливо?", "Да, справедливо", "Нет, ошибка", f"VAR: карточка, {who or team}"
        if "goal" in info or "offside" in info or t == "disallowed_goal":
            reason = " (офсайд)" if "offside" in info else ""
            return "Гол отменили. Правильно?", "Да, верно", "Нет, гол был", f"Отменён гол {who or team}{reason}"
        return "Верно ли решение VAR?", "Да", "Нет", f"Видеоповтор: {team}"
    if t == "penalty":
        return "Был ли пенальти?", "Да, был", "Нет, не было", f"Пенальти реализовал {who or team}"
    if t == "missed_penalty":
        return "Был ли пенальти?", "Да, был", "Нет, не было", f"Пенальти не забил {who or team}"
    if t == "red_card":
        second = "2nd" in info or "second" in info or "yellow" in info
        q = "Заслуженная вторая жёлтая?" if second else "Заслуженное удаление?"
        return q, "Да, по делу", "Нет, слишком строго", f"Красная карточка: {who or team}"
    return None


def _score_episode(event, match, votes: dict, referee_gap: dict) -> float:
    score = EVENT_WEIGHT.get(event.event_type, 0.0)
    if event.minute >= 75:
        score += 1.5
    margin = abs((match.home_score or 0) - (match.away_score or 0))
    if margin <= 1:
        score += 1.5  # решение могло повлиять на итог
    if match.is_derby:
        score += 1.0
    score += min(2.0, referee_gap.get(match.id, 0.0) / 2)
    score += math.log1p(votes.get(match.id, 0)) / 2
    return round(score, 2)


def pick_episode(matches=None):
    """Самый спорный эпизод тура: (event, match, тексты, индекс) или None."""
    from aggregates.models import RefereeMatchAggregate
    from engagement.models import DailyPoll
    from evaluations.models import EvaluationSession
    from events.models import MatchEvent

    matches = matches if matches is not None else last_round_matches()
    if not matches:
        return None
    by_id = {m.id: m for m in matches}
    used = set(DailyPoll.objects.filter(event__isnull=False).values_list("event_id", flat=True))
    votes = dict(
        EvaluationSession.objects.filter(match_id__in=by_id, status="completed")
        .values("match_id").annotate(n=Count("id")).values_list("match_id", "n")
    )
    referee_gap = {
        a.match_id: abs(a.home_fans_avg - a.away_fans_avg)
        for a in RefereeMatchAggregate.objects.filter(match_id__in=by_id, home_fans_avg__isnull=False, away_fans_avg__isnull=False)
    }
    best = None
    events = MatchEvent.objects.filter(match_id__in=by_id, event_type__in=EVENT_WEIGHT).select_related("player")
    for event in events:
        if event.id in used:
            continue
        match = by_id[event.match_id]
        texts = _episode_text(event, match)
        if not texts:
            continue
        score = _score_episode(event, match, votes, referee_gap)
        if best is None or score > best[3]:
            best = (event, match, texts, score)
    return best


def _duel_candidates(matches):
    """[(player, team_name, оценка, голосов, подпись)] лучших игроков тура."""
    from aggregates.models import PlayerMatchAggregate
    from aggregates.services import min_votes_for_display
    from matches.stat_ratings import stat_ratings_for_match
    from players.models import Player

    aggs = list(
        PlayerMatchAggregate.objects.filter(match__in=matches, total_votes__gte=min_votes_for_display())
        .select_related("player__team", "match__home_team", "match__away_team").order_by("-performance_score")[:DUEL_POOL]
    )
    if len(aggs) >= 2:
        return [
            (a.player, a.player.team.name if a.player.team else "", a.performance_score,
             f"оценка болельщиков {a.performance_score:.1f}", a.match)
            for a in aggs
        ]
    # Голосов мало — по оценке «по статистике».
    rated = []
    for match in matches:
        for player_id, rating in stat_ratings_for_match(match).items():
            if rating is not None:
                rated.append((rating, player_id, match))
    rated.sort(key=lambda r: r[0], reverse=True)
    rated = rated[:DUEL_POOL]
    players = Player.objects.in_bulk([pid for _, pid, _ in rated])
    return [
        (players[pid], players[pid].team.name if players[pid].team else "", rating, f"оценка по статистике {rating:.1f}", match)
        for rating, pid, match in rated if pid in players
    ]


def pick_duel(matches=None):
    """Пара с самыми близкими оценками из разных клубов (интрига) или None."""
    matches = matches if matches is not None else last_round_matches()
    cands = _duel_candidates(matches) if matches else []
    best = None
    for i, a in enumerate(cands):
        for b in cands[i + 1:]:
            if a[1] == b[1]:
                continue
            gap = abs(a[2] - b[2])
            # Близость важнее, при равенстве — сильнее пара.
            key = (round(gap, 2), -(a[2] + b[2]))
            if best is None or key < best[0]:
                best = (key, a, b)
    return (best[1], best[2]) if best else None


def create_poll(kind: str, force: bool = False):
    """Создать опрос недели; без force — не чаще раза в POLL_DAYS на тип."""
    from engagement.models import DailyPoll

    now = timezone.now()
    if not force and DailyPoll.objects.filter(kind=kind, created_at__gte=now - timedelta(days=POLL_DAYS - 1)).exists():
        return None
    matches = last_round_matches()
    closes = now + timedelta(days=POLL_DAYS)
    if kind == DailyPoll.KIND_EPISODE:
        found = pick_episode(matches)
        if not found:
            return None
        event, match, (question, opt_a, opt_b, what), score = found
        minute = f"{event.minute}+{event.added_time}'" if event.added_time else f"{event.minute}'"
        context = f"{match.home_team.name} {match.get_score_display()} {match.away_team.name}, {minute}. {what}"
        poll = DailyPoll(kind=kind, match=match, event=event, question=question, context=context[:255],
                         option_a=opt_a, option_b=opt_b, score=score, closes_at=closes)
    else:
        pair = pick_duel(matches)
        if not pair:
            return None
        a, b = pair
        tour = a[4].tour
        poll = DailyPoll(
            kind=kind, match=a[4], player_a=a[0], player_b=b[0],
            question=f"Кто сильнее сыграл в {tour}-м туре?" if tour else "Кто сильнее сыграл в туре?",
            context="Лучшие игроки двух разных матчей тура, оценки почти равные.",
            option_a=a[0].full_name[:80], option_b=b[0].full_name[:80],
            detail_a=f"{a[1]} · {a[3]}"[:120], detail_b=f"{b[1]} · {b[3]}"[:120],
            score=round(abs(a[2] - b[2]), 2), closes_at=closes,
        )
    poll.save()
    logger.info("daily poll %s: %s", kind, poll.question)
    return poll


def active_polls() -> list:
    """Последний открытый опрос каждого типа."""
    from engagement.models import DailyPoll

    now = timezone.now()
    polls = []
    for kind, _label in DailyPoll.KIND_CHOICES:
        poll = (DailyPoll.objects.filter(kind=kind, closes_at__gt=now)
                .select_related("match__home_team", "match__away_team", "player_a__team", "player_b__team")
                .order_by("-created_at").first())
        if poll:
            polls.append(poll)
    return polls


def results(poll) -> dict:
    counts = dict(poll.votes.values("choice").annotate(n=Count("id")).values_list("choice", "n"))
    a, b = counts.get("a", 0), counts.get("b", 0)
    total = a + b
    pct_a = round(a * 100 / total) if total else 50
    return {"a": a, "b": b, "total": total, "pct_a": pct_a, "pct_b": 100 - pct_a if total else 50}


def polls_for(user) -> list[dict]:
    """Опросы с моим голосом и результатами (результаты видны после голоса)."""
    from engagement.models import DailyPollVote

    polls = active_polls()
    mine = {}
    if getattr(user, "is_authenticated", False) and polls:
        mine = dict(DailyPollVote.objects.filter(user=user, poll__in=polls).values_list("poll_id", "choice"))
    return [{"poll": p, "my": mine.get(p.id), "results": results(p) if p.id in mine else None} for p in polls]


def vote(user, poll, choice: str) -> bool:
    """True — голос засчитан впервые (задание дня +)."""
    from engagement.models import DailyPollVote
    from engagement.quests import track

    if choice not in ("a", "b") or poll.closes_at <= timezone.now():
        return False
    try:
        with transaction.atomic():
            DailyPollVote.objects.create(poll=poll, user=user, choice=choice)
    except IntegrityError:
        return False
    track(user, "episode_vote" if poll.kind == poll.KIND_EPISODE else "duel_vote")
    return True
