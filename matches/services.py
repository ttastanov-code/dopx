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

# "ДНК матча" фаза 3 (docs/adr/0034-match-dna-phase3-antihero-fan-mood.md) —
# антигерой матча и настроение фанатов. Третий аудит (Codex, 2026-09-07)
# отдельно назвал их недостающими в шаринговой карточке. Порог "достаточно
# голосов за поддерживаемую команду, чтобы подавать процент как факт" —
# тот же принцип, что и у остальных гейтов этого модуля (REFEREE_DIVERGENCE_MIN_GAP,
# TURNING_POINT_MIN_RATIO): маленькая выборка не должна выглядеть как
# уверенное утверждение.
FAN_MOOD_MIN_VOTES = 3


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


def _describe_antihero(top_players: list, worst_players: list) -> dict | None:
    """"Антигерой матча" — первая строка уже отфильтрованного и
    отсортированного ПО ВОЗРАСТАНИЮ `worst_players` (тот же порог
    total_votes >= MIN_VOTES_FOR_DISPLAY, что и у top_players/_describe_hero —
    `matches/views.py::MatchDetailView` уже считает этот список для
    антифрод/качественной витрины, но до фазы 3 нигде его не показывал:
    данные были, витрины не было — та же формулировка, что и у
    _describe_turning_point до фазы 2).

    Не показываем антигероя, если он оказался ТЕМ ЖЕ игроком, что и герой —
    это происходит, когда после фильтра по MIN_VOTES_FOR_DISPLAY остался
    только один игрок: "антигерой" и "герой" совпадали бы, что вводит в
    заблуждение (не "второй полюс", а тот же самый человек).
    """
    if not worst_players:
        return None
    antihero_agg = worst_players[0]
    hero_agg = top_players[0] if top_players else None
    if hero_agg is not None and antihero_agg.player_id == hero_agg.player_id:
        return None
    return {"player": antihero_agg.player, "score": antihero_agg.performance_score}


def _describe_fan_mood(match_aggregate, fan_support: list) -> str:
    """"Настроение фанатов" — зрелищность матча (MatchAggregate.avg_entertainment,
    уже посчитана) плюс перекос трибун (ContextEvaluation.supported_team) —
    тот же агрегат, что matches/views.py уже считает для блока "За кого
    болели" (fan_support: до 2 строк вида {'supported_team__name', 'count'},
    отсортированных по count по убыванию), просто раньше не соединялся с
    зрелищностью в одну "настроенческую" фразу нигде на странице.

    :param fan_support: список dict'ов (не queryset — вызывающая сторона
        уже материализовала его для шаблона, здесь только читаем).
    :return: пустая строка, если голосов за поддерживаемую команду меньше
        FAN_MOOD_MIN_VOTES (шум) — то же решение "не гадать", что у
        _describe_referee_divergence/_describe_turning_point.
    """
    if not fan_support:
        return ""
    total = sum(row["count"] for row in fan_support)
    if total < FAN_MOOD_MIN_VOTES:
        return ""
    dominant = fan_support[0]
    pct = round(dominant["count"] / total * 100)
    return (
        f"Зрелищность матча болельщики оценили на {match_aggregate.avg_entertainment:.1f}/10, "
        f"{pct}% из проголосовавших за команду поддерживали {dominant['supported_team__name']}."
    )


def _describe_consensus_text(consensus_level: str | None) -> str:
    """Текстовая версия `_consensus_level` (та возвращает только 'high'/
    'medium'/'low'/None — для бейджа на странице этого достаточно, но
    шаринговая карточка (Pillow, `core/services/share_cards.py`) рисует
    обычный текст, не бейджи с цветом)."""
    if consensus_level == "high":
        return "Болельщики почти единодушны в оценке этого матча."
    if consensus_level == "low":
        return "Мнения о матче разошлись сильно — единого впечатления нет."
    if consensus_level == "medium":
        return "Мнения о матче разошлись умеренно."
    return ""


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
    worst_players: list | None = None, fan_support: list | None = None,
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
    :param worst_players: список PlayerMatchAggregate, отсортированный ПО
        ВОЗРАСТАНИЮ performance_score, тот же порог total_votes, что и
        top_players (фаза 3, для antihero) — matches/views.py уже считает
        его как `worst_players`, просто раньше нигде не показывал.
    :param fan_support: до 2 dict'ов {'supported_team__name', 'count'} —
        тот же агрегат, что matches/views.py считает для блока "За кого
        болели" (фаза 3, для fan_mood_text).
    :return: None, если голосов по матчу ещё нет вообще (нечего показывать —
        секция не должна рендериться пустой рамкой), иначе dict с ключами
        drama_index, drama_level ('high'/'medium'/'low'), momentum_points
        (list[str], может быть пустым), referee_divergence (str, может быть
        пустой), hero (dict{'player','score'} | None), antihero
        (dict{'player','score'} | None), turning_point_text (str, может быть
        пустой), consensus_level ('high'/'medium'/'low'/None), consensus_text
        (str, может быть пустой — текстовая версия consensus_level),
        fan_mood_text (str, может быть пустой), controversial_episode (str,
        может быть пустой).
    """
    if match_aggregate is None or match_aggregate.total_votes == 0:
        return None

    top_players = top_players or []
    consensus_level = _consensus_level(match_evaluations or [])

    return {
        "drama_index": match_aggregate.drama_index,
        "drama_level": _drama_level(match_aggregate.drama_index),
        "momentum_points": _describe_momentum(events),
        "referee_divergence": _describe_referee_divergence(match, referee_aggregate),
        "hero": _describe_hero(top_players),
        "antihero": _describe_antihero(top_players, worst_players or []),
        "turning_point_text": _describe_turning_point(match_aggregate),
        "consensus_level": consensus_level,
        "consensus_text": _describe_consensus_text(consensus_level),
        "fan_mood_text": _describe_fan_mood(match_aggregate, fan_support or []),
        "controversial_episode": _describe_controversial_episode(events, referee_aggregate),
    }
