# round_squad/services.py
"""
Алгоритм «Тура недели»: лучший состав 4-3-3 + тренер + «игрок тура» +
самый драматичный матч ОДНОГО тура (см. докстринг round_squad/models.py
про отличие от season_squad — сглаживание идёт по ГОЛОСАМ, а не по числу
матчей, потому что в туре у игрока почти всегда ровно один матч).

    round_score = (v / (v + C)) * raw_avg + (C / (v + C)) * pool_avg

где v — число голосов за кандидата В ЭТОМ ТУРЕ, raw_avg — средняя оценка
по этим голосам, pool_avg — средняя по всем кандидатам ТОГО ЖЕ пула
(взвешенная по голосам каждого), C — «виртуальные голоса»: чем их больше,
тем сильнее к pool_avg притягиваются кандидаты с малым числом голосов.
Ровно тот же принцип, что у season_squad._bayes_score, просто с другой
единицей измерения «объёма данных».
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
from round_squad.models import RoundBestXI, RoundBestXISlot

logger = logging.getLogger(__name__)

# «Виртуальные голоса» в байесовском сглаживании тура — см. докстринг
# модуля. Меньше, чем season_squad.SHRINKAGE_C=6.0 «виртуальных матчей»,
# осознанно: единица измерения тут другая (голоса, а не матчи), а типичный
# рядовой матч тура собирает 5-15 голосов за игрока — C=6 голосов даёт
# сопоставимую по силе поправку, не «усредняя всех в кашу» на достаточно
# оценённых матчах.
ROUND_VOTE_SHRINKAGE_C = 6.0

# Минимум голосов за кандидата в этом туре, чтобы он вообще участвовал в
# подборе — отдельно от сглаживания: 1-2 голоса (в т.ч. один троллинг-голос)
# не должны решать, кто «игрок тура».
ROUND_MIN_VOTES_FOR_CANDIDATE = 3

# Порог «данных достаточно» для кольца доверия на карточке — ниже, чем
# season_squad.CONFIDENT_VOTES_THRESHOLD=15, потому что весь объём данных
# тура физически меньше объёма данных сезона (один матч, а не десять+).
ROUND_CONFIDENT_VOTES_THRESHOLD = 10

# Порог «тур практически сыгран» для дефолтного выбора тура на странице
# без явного номера в URL (round_squad/views.py::_resolve_latest_tour).
# Не 100% — календарь КПЛ регулярно переносит 1-2 матча тура на другую
# дату, и требование "ВСЕ матчи тура завершены" держит дефолтную страницу
# на давно устаревшем туре, пока не доиграется последний хвост (баг,
# пойманный на прогоне 2026-08-22: реальный календарь на 22 туре, "все
# матчи завершены" держало страницу на туре 5 из-за одного зависшего
# переноса в туре 6). 0.75 — терпимо к одному перенесённому матчу из
# типичных 8 в туре КПЛ, но не пропускает туры, где сыграна лишь
# случайная горстка (как у тура с одним заранее сыгранным перенесённым
# матчем).
ROUND_CURRENT_TOUR_MIN_COMPLETION_RATIO = 0.75


def resolve_practically_closed_tour(season) -> int | None:
    """
    Номер тура, который на практике уже сыгран (см. докстринг константы
    ROUND_CURRENT_TOUR_MIN_COMPLETION_RATIO выше и историю багов в
    round_squad/views.py::_resolve_latest_tour, откуда эта функция
    вынесена сюда как публичный селектор, 2026-08-26).

    Используется в двух местах с разными требованиями к строгости:
    1) round_squad/views.py — дефолтный тур для страницы без явного
       номера в URL;
    2) core/context_processors.py::current_round_squad — запасной вариант
       для кнопки в шапке, когда ЕЩЁ НИ ОДИН тур не зафиксирован через
       RoundBestXI.is_final (тот флаг взводит периодическая Celery-задача
       раз в 15 минут — до её первого прогона после того, как тур
       практически завершился, кнопка иначе застряла бы на дефолтном
       "Тур недели", хотя реальный номер уже известен по данным Match).

    Сканирует туры от большего к меньшему и возвращает первый, где доля
    завершённых матчей проходит порог — см. полное обоснование алгоритма
    в _resolve_latest_tour.
    """
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
    """Кандидат тура, приведённый к общему виду (см. _rank_round_pool).
    object_id — строка (UUID BaseModel)."""
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
    """Тот же принцип, что season_squad._rank_pool, но pool_avg и вес
    сглаживания считаются по голосам, а не по числу матчей."""
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
    """Тур считается закрытым, когда у ВСЕХ его матчей voting_open_until
    в прошлом — новых голосов по сыгранным матчам тура физически больше не
    будет, донакручивать состав нечем. voting_open_until — обязательное
    поле Match (см. matches/models.py), поэтому проверка не зависит от
    статуса конкретного матча (finished/postponed/cancelled — не важно,
    важно только что окно голосования закрыто)."""
    now = timezone.now()
    matches = Match.objects.filter(season=season, tour=tour)
    return matches.exists() and not matches.filter(voting_open_until__gte=now).exists()


def _build_round_player_data(season, tour: int):
    """Возвращает (player_stats, pool_by_code):
      · player_stats — dict[player_id] -> RoundCandidate, ПЛОСКИЙ список
        независимо от позиции — источник для «игрока тура» (ранжируется
        целиком, а не внутри пула одной позиции).
      · pool_by_code — тот же набор кандидатов, сгруппированный по коду
        позиции (только те, у кого позиция вообще резолвится из состава) —
        источник для greedy-заполнения 11 слотов формации, тот же принцип,
        что season_squad._build_player_pool_by_code.
    """
    stats_rows = (
        PlayerMatchAggregate.objects
        .filter(match__season=season, match__tour=tour)
        .values("player_id")
        .annotate(raw_avg=Avg("performance_score"), votes=Sum("total_votes"))
    )
    lineup_rows = (
        MatchLineupPlayer.objects
        .filter(lineup__match__season=season, lineup__match__tour=tour)
        .exclude(position="")
        .values_list("player_id", "position", "field_position", "lineup__team__name")
    )
    # codes теперь список (см. resolve_lineup_codes) — обычно один элемент,
    # но храним как список, чтобы pool_by_code мог зарегистрировать
    # кандидата под всеми применимыми кодами без дублирования логики.
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
        # codes — список из resolve_lineup_codes(): обычно один элемент
        # ("D:L" для новых записей с известным field_position, либо голый
        # "D" для старых) — регистрируем под всеми, на случай если в
        # будущем resolve_lineup_codes станет возвращать несколько.
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
    """:return: (Match | None, drama_score | None, votes | None). Индекс
    драмы матча — MatchEvaluation.entertainment * MatchEvaluation.tension
    (см. evaluations/models.py::MatchEvaluation.drama_index), усреднённый
    по всем оценившим матч; F(...)*F(...) считается прямо в БД, а не по
    объекту, чтобы не тянуть все строки MatchEvaluation в память. Требуем
    ROUND_MIN_VOTES_FOR_CANDIDATE оценок матча — тот же принцип "не решать
    по 1-2 голосам", что и у игроков/тренера тура."""
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


def _build_round_explanation(label: str, candidate: RoundCandidate, score: float, is_confident: bool) -> str:
    sentence = (
        f"Рейтинг {score:.2f} на позиции «{label}» в этом туре — среднее по "
        f"{candidate.votes} голосам с поправкой на их число."
    )
    if is_confident:
        sentence += " Голосов достаточно, чтобы доверять этому месту."
    else:
        sentence += " Голосов пока немного — оценка может быть неточной."
    return sentence


def _apply_round_slot(round_best_xi: RoundBestXI, slot_code: str, candidate: RoundCandidate | None, score: float | None) -> None:
    order = BEST_XI_SLOT_DISPLAY_ORDER.get(slot_code, 99)
    label = BEST_XI_SLOT_LABELS.get(slot_code, slot_code)

    if candidate is None:
        RoundBestXISlot.objects.update_or_create(
            round_best_xi=round_best_xi, slot_code=slot_code,
            defaults=dict(
                order=order, content_type=None, object_id=None,
                occupant_name="", occupant_team_name="", occupant_photo_url="", occupant_profile_url="",
                round_score=None, votes_count=0, is_confident=False,
                explanation="Пока недостаточно голосов на этой позиции в этом туре.",
            ),
        )
        return

    is_confident = candidate.votes >= ROUND_CONFIDENT_VOTES_THRESHOLD
    RoundBestXISlot.objects.update_or_create(
        round_best_xi=round_best_xi, slot_code=slot_code,
        defaults=dict(
            order=order,
            content_type_id=candidate.content_type_id, object_id=candidate.object_id,
            occupant_name=candidate.name, occupant_team_name=candidate.team_name,
            occupant_photo_url=candidate.photo_url, occupant_profile_url=candidate.profile_url,
            round_score=score, votes_count=candidate.votes, is_confident=is_confident,
            explanation=_build_round_explanation(label, candidate, score, is_confident),
        ),
    )


def recompute_round(season, tour: int) -> RoundBestXI:
    """Точка входа — вызывается из round_squad/tasks.py (Celery Beat) и из
    админского действия «Пересчитать сейчас». Идемпотентна, как и
    season_squad.recompute_best_xi: безопасно вызывать чаще, чем нужно."""
    round_best_xi, _created = RoundBestXI.objects.get_or_create(season=season, tour=tour)
    if round_best_xi.is_final:
        logger.info("Тур %s сезона %s уже зафиксирован — пересчёт пропущен", tour, season)
        return round_best_xi

    now = timezone.now()
    player_stats, pool_by_code = _build_round_player_data(season, tour)
    coach_pool = _build_round_coach_pool(season, tour)

    # ---- 11 полевых слотов формации 4-3-3 — жадное распределение, тот же
    # принцип и тот же SLOT_PROCESSING_ORDER, что у season_squad. ----
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
        if ranked:
            top_candidate, top_score = ranked[0]
            assigned.add(top_candidate.object_id)
            _apply_round_slot(round_best_xi, slot_code, top_candidate, top_score)
        else:
            _apply_round_slot(round_best_xi, slot_code, None, None)

    # ---- Тренер тура — отдельный пул, не пересекается с игроками ----
    coach_ranked = _rank_round_pool(coach_pool)
    if coach_ranked:
        top_coach, coach_score = coach_ranked[0]
        _apply_round_slot(round_best_xi, "COACH", top_coach, coach_score)
    else:
        _apply_round_slot(round_best_xi, "COACH", None, None)

    # ---- «Игрок тура» — плоский пул, независимо от позиции/слота ----
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
        round_best_xi.player_of_round_explanation = (
            f"Лучший результат тура среди всех позиций — {player_score:.2f} "
            f"по {top_player.votes} голосам."
            + (" Голосов достаточно, чтобы доверять этому выбору." if is_confident
               else " Голосов пока немного — выбор может измениться.")
        )
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
            f"{dramatic_match.away_team.name} — самый высокий индекс зрелищности тура "
            f"({drama_score:.1f}, по {drama_votes} оценкам матча)."
        )
    else:
        round_best_xi.most_dramatic_match_explanation = ""

    # ---- Финализация: закрываем тур, если голосование по всем матчам
    # уже закрыто — донакручивать состав больше нечем (см. докстринг
    # round_squad/models.py про автоматический is_final). Функция выходит
    # раньше (см. самое начало), если round_best_xi.is_final уже был True
    # до этого вызова — значит, если мы дошли досюда и тур комплектен,
    # это ПЕРВЫЙ раз, когда тур закрывается, и именно здесь нужно один раз
    # разослать письмо с итогами (см. just_finalized ниже). ----
    just_finalized = False
    if _round_is_complete(season, tour):
        just_finalized = True
        round_best_xi.is_final = True
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
            # Генерация share-карточки — не критичный путь: тур должен
            # зафиксироваться и без картинки, если Pillow/диск подвели.
            logger.exception("Тур %s сезона %s: не удалось собрать share-карточку", tour, season)

    round_best_xi.last_computed_at = now
    round_best_xi.save()

    if just_finalized:
        # Ставим В ОЧЕРЕДЬ ПОСЛЕ .save() выше, не раньше: fan-out таска
        # читает RoundBestXI из БД по id (round_squad/tasks.py::
        # _send_round_results_email_chunk) — если поставить .delay() до
        # save(), воркер может забрать задачу раньше, чем транзакция
        # долетит до диска, и прочитать ещё старые (is_final=False) данные.
        try:
            from round_squad.tasks import send_round_results_notification

            send_round_results_notification.delay(str(round_best_xi.id))
        except Exception:
            # Рассылка — не критичный путь: тур должен остаться
            # зафиксированным, даже если Celery/брокер сейчас недоступны.
            logger.exception("Тур %s сезона %s: не удалось поставить в очередь рассылку итогов", tour, season)

    logger.info(
        "Тур %s сезона %s пересчитан (is_final=%s), игроков в составе: %d",
        tour, season, round_best_xi.is_final, len(assigned),
    )
    return round_best_xi
