# matches/services.py
"""
"ДНК матча" — фаза 1 (docs/PRODUCT_SCOPE_MATCH_DNA_AND_EXPLAINABILITY.md,
раздел 2). Не новые данные — новая композиция уже посчитанного: drama_index
(MatchAggregate, уже существовал), momentum-точки (группировка MatchEvent
по 15-минутным окнам — та же идея, что group-by в
evaluations/views.py::_compute_key_player_ids, только по времени, а не по
типу), и расхождение мнений болельщиков о судействе (RefereeMatchAggregate.
home_fans_avg/away_fans_avg — уже посчитанный сигнал, ранее нигде не
показывался пользователю как отдельная история).

Фаза 1 сознательно текстовая, без шаринговой карточки — см. "Риск" в
scope-документе: цена ошибки на карточке (дизайн/шрифты/легибилити) выше,
чем на текстовой секции, поэтому карточка (фаза 2) — только после проверки,
что эта секция вообще востребована.
"""
from __future__ import annotations

import statistics
from collections import defaultdict

MOMENTUM_WINDOW_MINUTES = 15
# Меньше двух событий в окне — не "момент", просто одно событие тайм-лайна,
# отдельно уже видное в блоке "События матча".
MOMENTUM_MIN_EVENTS = 2
# Разница средних оценок судейства двумя лагерями меньше этого — в пределах
# обычного шума мнений, не стоит подавать как "разошлись во мнениях".
REFEREE_DIVERGENCE_MIN_GAP = 1.5

DRAMA_HIGH_THRESHOLD = 60.0   # напр. avg_entertainment=8 * avg_tension=7.5
DRAMA_MEDIUM_THRESHOLD = 30.0  # напр. 6 * 5

# "ДНК матча" фаза 2 (docs/adr/0033-match-dna-phase2.md, раздел
# Match DNA) — герой матча/переломный момент/консенсус/спорный эпизод.
# Порог "достаточно голосов, чтобы называть кого-то героем" — тот же
# MIN_VOTES_FOR_DISPLAY, что уже применяется к top_players на странице
# матча (matches/views.py), передаём готовый отфильтрованный список, а не
# фильтруем повторно здесь.
TURNING_POINT_MIN_RATIO = 0.3
# Разброс (population stdev) композитной оценки матча voter'ом
# ((entertainment+tension+fairness)/3) по шкале 1-10. <= HIGH — мнения
# почти совпадают, >= LOW — мнения разошлись сильно, между — средний
# консенсус. Подобрано по тому же принципу, что и пороги драмы выше —
# ориентир на разницу в 1-2.5 балла по 10-балльной шкале, а не строгая
# статистическая калибровка (данных для неё пока недостаточно).
CONSENSUS_HIGH_STDEV = 1.0
CONSENSUS_LOW_STDEV = 2.5


def _drama_level(drama_index: float) -> str:
    if drama_index >= DRAMA_HIGH_THRESHOLD:
        return "high"
    if drama_index >= DRAMA_MEDIUM_THRESHOLD:
        return "medium"
    return "low"


def _pluralize_goals(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "гол"
    if 2 <= n % 10 <= 4 and not (12 <= n % 100 <= 14):
        return "гола"
    return "голов"


def _describe_momentum(events) -> list[str]:
    """events — итерируемый список MatchEvent (не queryset, чтобы не бить
    по БД повторно, если вызывающая сторона уже материализовала список для
    _match_events.html). Возвращает 0-2 строки, отсортированные по числу
    событий в 15-минутном окне (см. MOMENTUM_WINDOW_MINUTES)."""
    buckets: dict[int, list] = defaultdict(list)
    for event in events:
        window_start = (event.minute // MOMENTUM_WINDOW_MINUTES) * MOMENTUM_WINDOW_MINUTES
        buckets[window_start].append(event)

    scored = sorted(buckets.items(), key=lambda kv: len(kv[1]), reverse=True)
    points = []
    for window_start, bucket_events in scored[:2]:
        if len(bucket_events) < MOMENTUM_MIN_EVENTS:
            break
        window_end = window_start + MOMENTUM_WINDOW_MINUTES
        goals = sum(1 for e in bucket_events if e.event_type == "goal")
        if goals >= 2:
            points.append(f"{goals} {_pluralize_goals(goals)} в отрезке {window_start}–{window_end}'")
        else:
            points.append(f"{len(bucket_events)} событий в отрезке {window_start}–{window_end}'")
    return points


def _describe_referee_divergence(match, referee_agg) -> str:
    """RefereeMatchAggregate.home_fans_avg/away_fans_avg — уже посчитанный
    сигнал (aggregates/tasks.py::recalculate_referee_aggregates), просто
    никогда не выводился пользователю как отдельная история. Гейт "меньше
    2 сегментов — сравнивать не с чем" — тот же принцип, что у
    core/templatetags/rating_extras.py::bias_segment_text."""
    if referee_agg is None:
        return ""
    home_avg = referee_agg.home_fans_avg
    away_avg = referee_agg.away_fans_avg
    if home_avg is None or away_avg is None:
        return ""
    gap = abs(home_avg - away_avg)
    if gap < REFEREE_DIVERGENCE_MIN_GAP:
        return ""
    return (
        f"Болельщики {match.home_team.name} и {match.away_team.name} разошлись "
        f"во мнениях о судействе на {gap:.1f} балла."
    )


def _describe_hero(top_players: list) -> dict | None:
    """"Герой матча" — просто первая строка уже отфильтрованного и
    отсортированного `top_players` (matches/views.py: PlayerMatchAggregate
    с total_votes >= MIN_VOTES_FOR_DISPLAY, order_by('-performance_score')) —
    никакого нового запроса, тот же список, что уже рендерится в "Топ
    игроков матча" чуть ниже на странице."""
    if not top_players:
        return None
    hero_agg = top_players[0]
    return {"player": hero_agg.player, "score": hero_agg.performance_score}


def _describe_turning_point(match_aggregate) -> str:
    """turning_point_ratio — уже посчитанное поле MatchAggregate (доля
    голосовавших, отметивших чекбокс "был переломный момент?" на
    match_final.html), посчитанное, но нигде не показанное пользователю до
    этой правки (см. docs/CODEX_AUDIT_RESPONSE_2026-09-07.md — данные есть,
    витрины не было)."""
    ratio = match_aggregate.turning_point_ratio
    if ratio < TURNING_POINT_MIN_RATIO:
        return ""
    pct = round(ratio * 100)
    return f"{pct}% зрителей отметили явный переломный момент в этом матче."


def _consensus_level(match_evaluations: list) -> str | None:
    """"Уровень консенсуса" — population stdev композитной оценки
    ((entertainment+tension+fairness)/3) КАЖДОГО голосовавшего, а не stdev
    уже усреднённых чисел (это было бы стандартной ошибкой средних, не
    разбросом мнений). :param match_evaluations: сырые MatchEvaluation этого
    матча (matches/views.py передаёт `.only(...)` по трём полям — читать
    их всё равно нужно построчно, агрегат MatchAggregate такого разброса не
    хранит). :return: None при < 2 голосах (сравнивать не с чем)."""
    composites = [(e.entertainment + e.tension + e.fairness) / 3 for e in match_evaluations]
    if len(composites) < 2:
        return None
    stdev = statistics.pstdev(composites)
    if stdev <= CONSENSUS_HIGH_STDEV:
        return "high"
    if stdev >= CONSENSUS_LOW_STDEV:
        return "low"
    return "medium"


def _describe_controversial_episode(events: list, referee_aggregate) -> str:
    """"Главный спорный эпизод" — эвристика на основе уже существующих
    сигналов, НЕ отдельное голосование за "самый спорный момент" (такого
    механизма в продукте нет, честно отмечаем это ограничение здесь, а не
    выдаём эвристику за точный факт):

    1. Отменённый гол (event_type='disallowed_goal') — самый однозначный
       прокси "спорности" из доступных: решение под вопросом ПО
       ОПРЕДЕЛЕНИЮ (VAR/офсайд/повтор), не требует доп. сигналов.
    2. Иначе, только если фанаты разошлись во мнениях о судействе
       (см. _describe_referee_divergence — тот же порог REFEREE_DIVERGENCE_MIN_GAP,
       переиспользуем как признак "было о чём спорить") — берём первую
       красную карточку матча как вероятную причину этого расхождения.
       Без гейта на расхождение красная карточка сама по себе слишком
       обычное событие футбола, чтобы называть её "спорной".
    3. Если ни то, ни другое — возвращаем "", не гадаем.
    """
    disallowed = [e for e in events if e.event_type == "disallowed_goal"]
    if disallowed:
        event = disallowed[0]
        who = f" ({event.player})" if event.player_id else ""
        return f"Отменённый гол на {event.display_minute}'{who} — главный спорный эпизод матча."

    if referee_aggregate is not None:
        home_avg, away_avg = referee_aggregate.home_fans_avg, referee_aggregate.away_fans_avg
        if home_avg is not None and away_avg is not None and abs(home_avg - away_avg) >= REFEREE_DIVERGENCE_MIN_GAP:
            red_cards = [e for e in events if e.event_type == "red_card"]
            if red_cards:
                event = red_cards[0]
                who = f" ({event.player})" if event.player_id else ""
                gap = abs(home_avg - away_avg)
                return (
                    f"Красная карточка на {event.display_minute}'{who} — вероятно, "
                    f"самый спорный момент матча (мнения о судействе разошлись на {gap:.1f} балла)."
                )
    return ""


def build_match_dna(
    match, match_aggregate, events, referee_aggregate=None,
    match_evaluations: list | None = None, top_players: list | None = None,
) -> dict | None:
    """Собирает контекст для секции "ДНК матча" на странице матча.

    :param match_aggregate: уже полученный `match.aggregate` (MatchAggregate
        либо None) — не делаем повторный запрос, вызывающая сторона
        (matches/views.py::MatchDetailView) его и так уже получает.
    :param events: список/queryset MatchEvent этого матча (для momentum и,
        с фазы 2, controversial_episode).
    :param referee_aggregate: `match.referee_aggregates.first()` либо None.
    :param match_evaluations: сырые MatchEvaluation этого матча (фаза 2,
        для consensus_level) — `None`/пустой список даёт consensus_level=None,
        секция при этом не ломается, просто не показывает эту строку.
    :param top_players: уже отфильтрованный/отсортированный список
        PlayerMatchAggregate (фаза 2, для hero) — тот же список, что
        matches/views.py передаёт в шаблон как `top_players`.
    :return: None, если голосов по матчу ещё нет вообще (нечего показывать —
        секция не должна рендериться пустой рамкой), иначе dict с ключами
        drama_index, drama_level ('high'/'medium'/'low'), momentum_points
        (list[str], может быть пустым), referee_divergence (str, может быть
        пустой), hero (dict{'player','score'} | None), turning_point_text
        (str, может быть пустой), consensus_level ('high'/'medium'/'low'/None),
        controversial_episode (str, может быть пустой).
    """
    if match_aggregate is None or match_aggregate.total_votes == 0:
        return None

    return {
        "drama_index": match_aggregate.drama_index,
        "drama_level": _drama_level(match_aggregate.drama_index),
        "momentum_points": _describe_momentum(events),
        "referee_divergence": _describe_referee_divergence(match, referee_aggregate),
        "hero": _describe_hero(top_players or []),
        "turning_point_text": _describe_turning_point(match_aggregate),
        "consensus_level": _consensus_level(match_evaluations or []),
        "controversial_episode": _describe_controversial_episode(events, referee_aggregate),
    }
