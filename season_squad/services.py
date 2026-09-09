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

# "Почему он в сборной?" (docs/PRODUCT_SCOPE_MATCH_DNA_AND_EXPLAINABILITY.md,
# раздел 1) — тот же список заметных событий, что и в
# evaluations/views.py::_compute_key_player_ids (docs/adr/0006): самый
# дешёвый доступный прокси "заметности" на основе уже посчитанных данных,
# без новой метрики. Не переиспользуем импортом из evaluations/ напрямую —
# evaluations/views.py тянет за собой Django-view-слой (сессии вайзарда,
# HttpRequest), совершенно не нужный здесь для одной константы.
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
    """player_id (строкой) -> самый частый (мода) КОД позиции этого игрока
    в сезоне, по фактическим составам (MatchLineupPlayer, включая скамейку
    — амплуа не зависит от того, вышел человек с первых минут).

    2026-08-23: код теперь берётся через resolve_lineup_codes(position,
    field_position) — там, где известна сторона поля (см.
    players/positions.py), это комбинированный код "D:L", иначе — голый
    амплуа-код, как раньше. Мода считается по этим кодам напрямую, а не
    по голому амплуа: если игрок почти всегда выходил на левом фланге
    защиты, его код сезона — "D:L", и он корректно конкурирует именно за
    LB, а не размывается в общий пул CB1/CB2/RB/LB."""
    rows = (
        MatchLineupPlayer.objects
        .filter(lineup__match__season=season)
        .exclude(position="")
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
    """player_id (строкой) -> название команды, за которую игрок выходил в
    САМОМ ПОЗДНЕМ по дате матче этого сезона (MatchLineupPlayer, а не
    player.team — текущая команда в справочнике). Продуктовое ревью
    2026-08-22: игрок мог перейти в другой клуб в середине или сразу
    после сезона — карточка "Сборной DOPX" должна показывать клуб, за
    который он реально заработал этот рейтинг В ЭТОМ СЕЗОНЕ, а не куда
    он сейчас числится в БД. order_by('player_id', '-...start_time') +
    "первая встреченная строка на игрока" — тот же трюк, что мода в
    _player_season_position, только вместо Counter берём просто самую
    свежую запись (без доп. GROUP BY/window-function)."""
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
    """То же самое, что _player_season_team_name, но для тренеров — у них
    нет отдельной модели "состав на матч", привязка тренер/команда идёт
    прямо через Match.home_coach/away_coach + Match.home_team/away_team.
    Именно поэтому Codex-ревью отдельно предупреждал "требует осторожности
    из-за ограничений данных KFF": здесь привязка грубее (только на уровне
    матча целиком, не факта присутствия), но источник для КОНКРЕТНОГО
    матча всё равно надёжнее, чем текущее coach.team на момент пересчёта."""
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
            # Клуб В ЭТОМ СЕЗОНЕ (по факту составов), а не текущий
            # player.team — см. докстринг _player_season_team_name.
            # Фолбэк на player.team — на случай если у игрока есть
            # PlayerMatchAggregate, но почему-то нет ни одной строки
            # MatchLineupPlayer в этом сезоне (не должно происходить в
            # норме, т.к. агрегаты считаются по составам, но не полагаемся
            # на это молча).
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
            team_name=team_name_by_coach.get(cid) or (coach.team.name if coach.team else ""),
            photo_url=coach.photo.url if coach.photo else "",
            profile_url=reverse("coaches:detail", args=[coach.id]),
            raw_avg=raw_avg,
            matches=row["matches"] or 0,
            votes=row["votes"] or 0,
        ))
    return pool


def _build_referee_pool(season, referee_ct: ContentType) -> list[Candidate]:
    """Продуктовое ревью 2026-08-22: "лучший судья" не должен считаться
    только по decision_quality ("качество решений") — формула учитывает
    ещё influence_score ("влияние на матч": хороший судья по общему
    футбольному консенсусу должен быть "невидимым") и fairness с
    MatchEvaluation ("Справедливость" матча — воспринимаемая
    несправедливость почти всегда именно про судейство):

        0.6 * decision_quality + 0.3 * fairness + 0.1 * (10 - influence/10)

    2026-08-23: сама формула и её взвешенный/винзоризованный расчёт
    ПЕРЕЕХАЛИ в aggregates/tasks.py::recalculate_referee_aggregates —
    раньше она дублировалась ЗДЕСЬ (и второй раз ещё в referees/views.py)
    заново на каждый пересчёт, каждый раз заново примитивным Avg() без
    веса пользователя/винзоризации. Теперь читаем уже готовый
    RefereeMatchAggregate.performance_score и просто усредняем по матчам
    сезона (не по голосам — так матч с 20 голосами не "перевешивает" матч
    с 3 голосами, тот же принцип, что у PlayerMatchAggregate).
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
    """"Сравнение с ближайшим конкурентом" (docs/adr/0032-squad-explainability-v2.md,
    продуктовый запрос по итогам второго аудита). `runner_up` — ранг №2 из
    того же пула, что и occupant слота (см. `_rank_pool` — уже посчитанный,
    отсортированный список, второй элемент даром, без нового запроса).

    Не показываем фразу, если runner_up отсутствует (пул из одного
    кандидата — сравнивать не с кем) или если gap получился <= 0 — по
    построению occupant это ранг №1, поэтому такое означает не реальную
    ничью, а гонку за третьим знаком после округления `_rank_pool` (`round`
    в scored), и вводящая в заблуждение фраза "обошёл на 0.00 балла" хуже,
    чем её отсутствие."""
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
    """Собирает ЕДИНОЕ пояснение для тултипа на карточке — раньше confidence
    (бейдж "достаточно/мало данных") и rank_change (бейдж "вошёл в состав"/
    "↑ N") были отдельными текстовыми бейджами прямо на карточке, что и
    вызвало жалобу продакта ("вошёл в состав — непонятно о чём"): сама по
    себе фраза не объясняет, что это про место в рейтинге. Теперь вся эта
    информация — один связный текст под одной иконкой-подсказкой, а на
    карточке остаётся только цвет кольца аватара (тонкий сигнал, не текст).

    2026-09-09 (жалоба пользователя со скриншотом тултипа: "куча тире
    дефисов явный ИИ паттерн и текст как куча букв без структуры и
    нечитабельно") — раньше все предложения склеивались ПРОБЕЛОМ в один
    плотный абзац внутри узкого 240px-пузыря (components/_tooltip_icon.html),
    и почти каждое предложение отдельно использовало " — " как разделитель
    внутри себя — при вёрстке в одну строку это давало сплошную стену из
    тире. Теперь: (1) каждый факт — отдельная строка, СОЕДИНЯЮТСЯ ПЕРЕНОСОМ
    "\\n" (тултип рендерит их раздельными строками, см. white-space:
    pre-line в _tooltip_icon.html), (2) формулировки внутри предложений по
    возможности без тире — двоеточие вместо " — "."""
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
    """"Почему он в сборной?" — расширение _build_explanation ниже.
    season_score — одно число ("средняя с поправкой на объём выборки"), но
    само по себе не объясняет, ЗА ЧТО именно игрок получил высокие оценки.
    Берём топ-N матчей по performance_score из тех, что вошли в усреднение
    (те же PlayerMatchAggregate, что уже посчитаны _build_player_pool_by_code
    — никаких новых полей/агрегатов), с датой/соперником и заметными
    событиями (см. NOTABLE_EVENT_TYPES выше). Соперник определяется через
    факт участия в составе (MatchLineupPlayer), а не через player.team —
    та же причина, что в _player_season_team_name (переход в другой клуб
    в середине сезона не должен искажать историю ПРОШЛЫХ матчей)."""
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

    # 2026-09-09 (та же жалоба на "стену тире") — раньше строка выглядела
    # "8.9 — 20.03 vs Иртыш; 8.8 — 19.04 vs Атырау." (тире + "vs" + ";" —
    # три разных разделителя вперемешку). Теперь каждый матч — своя строка
    # с маркером "·", факты внутри строки — через скобки/запятую, без тире.
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
        # Обогащение только для игроков — у тренера/судьи нет
        # персональных MatchEvent (голы/карточки привязаны к players.Player).
        top_matches_text = _describe_top_matches(candidate.object_id, season)
        if top_matches_text:
            # Пустая строка — визуальный отступ между блоком "почему в
            # составе" и блоком "лучшие матчи" в тултипе (white-space:
            # pre-line превращает "\n\n" в пустую строку, а не схлопывает).
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
    # 4-й элемент — runner_up (ранг №2 того же пула, для "Обошёл ближайшего
    # конкурента" в _build_explanation) — уже посчитан внутри `ranked`,
    # просто прокидываем дальше без нового запроса.
    slot_results: list[tuple[str, Candidate | None, float | None, tuple[Candidate, float] | None]] = []

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
            runner_up = ranked[1] if len(ranked) > 1 else None
            slot_results.append((slot_code, top_candidate, top_score, runner_up))
        else:
            slot_results.append((slot_code, None, None, None))

    # ---- Тренер и судья — отдельные пулы, не пересекаются с игроками ----
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
