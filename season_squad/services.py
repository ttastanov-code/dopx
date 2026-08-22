# season_squad/services.py
"""
Алгоритм "Живая сборная сезона": пересчитывает лучший состав 4-3-3 +
тренера + судью на основе накопленных оценок пользователей.

Ключевая методологическая проблема, которую явно обозначил продакт: "нельзя
брать просто максимальную среднюю оценку — игрок с одним матчем и 10/10
обгонит игрока, который стабильно играл весь сезон". Решение — байесовское
сглаживание (тот же принцип, что у IMDB weighted rating):

    season_score = (m / (m + C)) * raw_avg + (C / (m + C)) * pool_avg

где m — число оцененных матчей игрока/тренера/судьи в сезоне, raw_avg — его
собственное среднее по этим матчам, pool_avg — среднее по всем кандидатам
в ТОМ ЖЕ пуле (вратарей сравниваем со вратарями, а не со всеми подряд,
взвешенное по числу матчей каждого — иначе один кандидат с 1 матчем имел
бы такой же вес в pool_avg, как игрок с 20 матчами), C — константа
"виртуальных матчей": чем она больше, тем сильнее к pool_avg притягиваются
кандидаты с малым числом матчей. При m >> C формула стремится к чистому
raw_avg (устоявшийся игрок оценивается по своим фактическим результатам);
при m << C — почти целиком к pool_avg (один суперматч не выносит игрока
в топ). Это ровно то же самое семейство методов, что у "средневзвешенного
рейтинга" IMDB/BGG — публично объяснимо и не выглядит как чёрный ящик,
важно для раздела "Как считается?" на странице.

Слот занимает игрок с максимальным season_score в своём пуле кандидатов;
после назначения игрок исключается из пулов ВСЕХ ОСТАЛЬНЫХ слотов этого же
прогона (см. players/positions.py::SLOT_PROCESSING_ORDER — порядок
обработки слотов специально идёт от узких амплуа к широким, чтобы игрок
с точным кодом позиции не терялся в общем "фолбэк"-пуле).

Про rank_change (стрелки ↑/«вошёл в состав» на карточках) — см. докстринг
season_squad/models.py::SeasonBestXISlot и docstring ниже у _apply_slot.
"""
from __future__ import annotations

import logging
from collections import Counter, defaultdict
from dataclasses import dataclass

from django.contrib.contenttypes.models import ContentType
from django.db.models import Avg, Count, Sum
from django.urls import reverse
from django.utils import timezone

from aggregates.models import CoachMatchAggregate, PlayerMatchAggregate
from aggregates.services import CONFIDENT_VOTES_THRESHOLD
from coaches.models import Coach
from evaluations.models import RefereeEvaluation
from lineups.models import MatchLineupPlayer
from players.models import Player
from players.positions import (
    BEST_XI_SLOT_DISPLAY_ORDER,
    BEST_XI_SLOT_LABELS,
    SLOT_PROCESSING_ORDER,
    clean_position_code,
)
from referees.models import Referee
from season_squad.models import SeasonBestXI, SeasonBestXISlot, SeasonPositionRanking

logger = logging.getLogger(__name__)

# "Виртуальные матчи" в байесовском сглаживании — см. докстринг модуля.
# 6 подобрано эмпирически: в первые недели сезона у большинства игроков
# 1-3 оцененных матча, C=6 достаточно, чтобы не пускать в топ игрока
# с одним матчем 10/10, но не "усредняет всех в кашу" к середине сезона,
# когда у стабильных игроков уже 10+ матчей.
SHRINKAGE_C = 6.0

# Минимум оцененных матчей, чтобы кандидат вообще участвовал в подборе
# состава — отдельно от числа голосов: даже 20 голосов за один-единственный
# матч не делает игрока "стабильным весь сезон".
MIN_MATCHES_FOR_CANDIDATE = 2

# Сколько последних "партий" (batch = один computed_at на весь прогон)
# ранжирования хранить в SeasonPositionRanking — старше чистит recompute.
RANKING_BATCHES_TO_KEEP = 5

# Сколько кандидатов пула сохранять в снимок ранжирования — карточкам
# нужен только occupant (ранг 1), топ-10 с запасом на будущий блок
# "кто ещё претендует на позицию".
RANKING_POOL_DEPTH = 10


@dataclass
class Candidate:
    """Игрок / тренер / судья, приведённые к общему виду для подбора
    состава (см. _rank_pool). object_id — строка (UUID БазовыхMoделей)."""
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
    """Считает Байес-скор для каждого кандидата пула (pool_avg — среднее
    raw_avg по ЭТОМУ ЖЕ пулу, взвешенное по числу матчей каждого) и
    возвращает список (candidate, score), отсортированный по убыванию."""
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
    """player_id (строкой) -> самый частый (мода) сырой код позиции этого
    игрока в сезоне, по фактическим составам (MatchLineupPlayer, включая
    скамейку — амплуа не зависит от того, вышел человек с первых минут)."""
    rows = (
        MatchLineupPlayer.objects
        .filter(lineup__match__season=season)
        .exclude(position="")
        .values_list("player_id", "position")
    )
    counters: dict[str, Counter] = defaultdict(Counter)
    for player_id, position in rows:
        counters[str(player_id)][clean_position_code(position)] += 1
    return {pid: counter.most_common(1)[0][0] for pid, counter in counters.items() if counter}


def _build_player_pool_by_code(season, player_ct: ContentType) -> dict[str, list[Candidate]]:
    position_by_player = _player_season_position(season)
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
            team_name=player.team.name if player.team else "",
            photo_url=player.photo.url if player.photo else "",
            profile_url=reverse("players:detail", args=[player.id]),
            raw_avg=row["raw_avg"] or 0.0,
            matches=row["matches"] or 0,
            votes=row["votes"] or 0,
        ))
    return pool


def _build_coach_pool(season, coach_ct: ContentType) -> list[Candidate]:
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
        # Среднее по средним = среднее по матчам благодаря линейности
        # (average_score каждого матча — уже (t+s+m+i)/4), пересчитывать
        # через сырые оценки построчно не нужно.
        raw_avg = (
            (row["avg_t"] or 0.0) + (row["avg_s"] or 0.0)
            + (row["avg_m"] or 0.0) + (row["avg_i"] or 0.0)
        ) / 4
        pool.append(Candidate(
            content_type_id=coach_ct.id,
            object_id=cid,
            name=coach.full_name,
            team_name=coach.team.name if coach.team else "",
            photo_url=coach.photo.url if coach.photo else "",
            profile_url=reverse("coaches:detail", args=[coach.id]),
            raw_avg=raw_avg,
            matches=row["matches"] or 0,
            votes=row["votes"] or 0,
        ))
    return pool


def _build_referee_pool(season, referee_ct: ContentType) -> list[Candidate]:
    # У RefereeEvaluation нет собственного агрегата за матч (в отличие от
    # игроков/тренеров) — считаем средний decision_quality за КАЖДЫЙ матч
    # отдельно (match_avg), а потом усредняем по матчам, а не по голосам:
    # так матч с 20 голосами не "перевешивает" матч с 3 голосами при
    # подсчёте raw_avg, ровно как у PlayerMatchAggregate.
    match_level = (
        RefereeEvaluation.objects
        .filter(match__season=season, match__referee__isnull=False)
        .values("match__referee_id", "match_id")
        .annotate(match_avg=Avg("decision_quality"), match_votes=Count("id"))
    )
    agg: dict[str, dict] = defaultdict(lambda: {"matches": 0, "votes": 0, "sum_avg": 0.0})
    for row in match_level:
        rid = str(row["match__referee_id"])
        bucket = agg[rid]
        bucket["matches"] += 1
        bucket["votes"] += row["match_votes"]
        bucket["sum_avg"] += row["match_avg"]

    referees = {str(r.id): r for r in Referee.objects.filter(is_active=True)}
    pool = []
    for rid, bucket in agg.items():
        referee = referees.get(rid)
        if not referee or bucket["matches"] == 0:
            continue
        pool.append(Candidate(
            content_type_id=referee_ct.id,
            object_id=rid,
            name=referee.full_name,
            team_name="",
            photo_url=referee.photo.url if referee.photo else "",
            profile_url=reverse("referees:detail", args=[referee.id]),
            raw_avg=bucket["sum_avg"] / bucket["matches"],
            matches=bucket["matches"],
            votes=bucket["votes"],
        ))
    return pool


def _build_explanation(
    slot_code: str,
    candidate: Candidate,
    score: float,
    is_confident: bool,
    rank_change: str,
    rank_change_delta: int | None,
) -> str:
    """Собирает ЕДИНОЕ пояснение для тултипа на карточке — раньше confidence
    (бейдж "достаточно/мало данных") и rank_change (бейдж "вошёл в состав"/
    "↑ N") были отдельными текстовыми бейджами прямо на карточке, что и
    вызвало жалобу продакта ("вошёл в состав — непонятно о чём"): сама по
    себе фраза не объясняет, что это про место в рейтинге. Теперь вся эта
    информация — один связный текст под одной иконкой-подсказкой, а на
    карточке остаётся только цвет кольца аватара (тонкий сигнал, не текст)."""
    label = BEST_XI_SLOT_LABELS.get(slot_code, slot_code)
    sentences = [
        f"Рейтинг {score:.2f} на позиции «{label}» — среднее за сезон с поправкой "
        f"на объём выборки ({candidate.matches} матчей, {candidate.votes} голосов)."
    ]

    if is_confident:
        sentences.append("Голосов достаточно, чтобы доверять этому месту в составе.")
    else:
        sentences.append("Голосов пока немного — место может измениться, когда их станет больше.")

    if rank_change == SeasonBestXISlot.RANK_CHANGE_NEW:
        sentences.append("Занял место в составе по итогам последнего пересчёта.")
    elif rank_change == SeasonBestXISlot.RANK_CHANGE_UP and rank_change_delta:
        matches_word = "место" if rank_change_delta == 1 else "места"
        sentences.append(f"Поднялся на {rank_change_delta} {matches_word} с прошлого пересчёта.")

    return " ".join(sentences)


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
) -> None:
    """Записывает/обновляет денормализованную карточку слота.

    rank_change: occupant слота — по определению ранг №1 в своём пуле НА
    ЭТОТ МОМЕНТ, поэтому относительно предыдущего пересчёта он либо "уже
    был №1" (SAME), либо "поднялся с ранга N" (UP, delta=N-1), либо "не
    участвовал в прошлом пересчёте вообще" (NEW — не хватало матчей или
    отсутствовал в лиге). DOWN технически недостижим для occupant'а этим
    алгоритмом (см. докстринг season_squad/models.py) — оставлен в схеме
    для возможного будущего блока "кто вылетел из состава".
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
                explanation="Пока недостаточно оценённых матчей на этой позиции — "
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
            explanation=_build_explanation(slot_code, candidate, score, is_confident, rank_change, delta),
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
    """Точка входа — вызывается из season_squad/tasks.py (Celery Beat) и из
    админского действия "Пересчитать сейчас". Идемпотентна: безопасно
    вызывать чаще, чем раз в период — если данные не изменились, состав
    просто перезапишется теми же значениями (rank_change корректно
    схлопнется в SAME)."""
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

    # Батч предыдущего пересчёта — снимаем ДО записи нового, иначе он же
    # окажется "предыдущим самому себе".
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
    slot_results: list[tuple[str, Candidate | None, float | None]] = []

    # ---- 11 полевых слотов формации 4-3-3 — жадное распределение ----
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
            slot_results.append((slot_code, top_candidate, top_score))
        else:
            slot_results.append((slot_code, None, None))

    # ---- Тренер и судья — отдельные пулы, не пересекаются с игроками ----
    for slot_code, pool in (("COACH", coach_pool), ("REFEREE", referee_pool)):
        ranked = _rank_pool(pool)
        _store_ranking_batch(ranking_buffer, best_xi, slot_code, ranked, now)
        if ranked:
            top_candidate, top_score = ranked[0]
            slot_results.append((slot_code, top_candidate, top_score))
        else:
            slot_results.append((slot_code, None, None))

    SeasonPositionRanking.objects.bulk_create(ranking_buffer, batch_size=200)

    for slot_code, candidate, score in slot_results:
        _apply_slot(best_xi, slot_code, candidate, score, previous_ranks)

    _prune_old_rankings(best_xi)

    best_xi.last_computed_at = now
    best_xi.save(update_fields=["last_computed_at"])
    logger.info("Живая сборная сезона %s пересчитана: %d слотов заполнено", season, len(assigned) + 2)
    return best_xi


def finalize_best_xi(season) -> SeasonBestXI:
    """Замораживает текущую живую сборную как итоговую — вызывается стаффом
    вручную из админки после окончания сезона и закрытия последних
    голосований (см. season_squad/admin.py). После этого recompute_best_xi
    для этого сезона становится no-op."""
    best_xi = SeasonBestXI.objects.get(season=season)
    best_xi.is_final = True
    best_xi.finalized_at = timezone.now()
    best_xi.save(update_fields=["is_final", "finalized_at"])
    return best_xi
