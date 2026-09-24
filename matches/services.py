# matches/services.py
"""«ДНК матча» и тексты для карточек матчей.

Собирает уже посчитанные сигналы (drama_index, события, оценки судейства,
топ/антитоп игроков, настроение фанатов) в короткие фразы. Новых данных не считает.
"""
from __future__ import annotations

import statistics
from collections import defaultdict

from django.db.models import Count, Q

from matches.models import Match, MatchReaction

MOMENTUM_WINDOW_MINUTES = 15
# Меньше двух событий в окне — не «момент».
MOMENTUM_MIN_EVENTS = 2
# Разница оценок судейства лагерями меньше этого — шум.
REFEREE_DIVERGENCE_MIN_GAP = 1.5

DRAMA_HIGH_THRESHOLD = 60.0
DRAMA_MEDIUM_THRESHOLD = 30.0

# Герой/антигерой берутся из уже отфильтрованных top/worst_players (MIN_VOTES_FOR_DISPLAY).
TURNING_POINT_MIN_RATIO = 0.3
# Пороги консенсуса по разбросу (stdev) оценки матча голосующими.
CONSENSUS_HIGH_STDEV = 1.0
CONSENSUS_LOW_STDEV = 2.5

# Минимум голосов по поддерживаемой команде для «настроения фанатов».
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
    """0-2 самых насыщенных событиями 15-минутных окна. events — список, не queryset."""
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
    """Расхождение оценок судейства фанатами хозяев и гостей."""
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
    """Герой матча — первый из top_players."""
    if not top_players:
        return None
    hero_agg = top_players[0]
    return {"player": hero_agg.player, "score": hero_agg.performance_score}


def _describe_antihero(top_players: list, worst_players: list) -> dict | None:
    """Антигерой — первый из worst_players. Не показываем, если это тот же игрок, что герой."""
    if not worst_players:
        return None
    antihero_agg = worst_players[0]
    hero_agg = top_players[0] if top_players else None
    if hero_agg is not None and antihero_agg.player_id == hero_agg.player_id:
        return None
    return {"player": antihero_agg.player, "score": antihero_agg.performance_score}


def _describe_fan_mood(match_aggregate, fan_support: list) -> str:
    """Настроение фанатов: зрелищность + за кого болели.

    :param fan_support: до 2 dict {'supported_team__name', 'count'}.
    :return: пустая строка, если голосов мало.
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
    """Текстовая версия _consensus_level (для карточки-картинки)."""
    if consensus_level == "high":
        return "Болельщики почти единодушны в оценке этого матча."
    if consensus_level == "low":
        return "Мнения о матче разошлись сильно — единого впечатления нет."
    if consensus_level == "medium":
        return "Мнения о матче разошлись умеренно."
    return ""


def _describe_turning_point(match_aggregate) -> str:
    """Переломный момент — доля отметивших его в финальной оценке."""
    ratio = match_aggregate.turning_point_ratio
    if ratio < TURNING_POINT_MIN_RATIO:
        return ""
    pct = round(ratio * 100)
    return f"{pct}% зрителей отметили явный переломный момент в этом матче."


def _consensus_level(match_evaluations: list) -> str | None:
    """Уровень консенсуса по разбросу оценок матча голосующими. None при < 2 голосах."""
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
    """Главный спорный эпизод (эвристика):
    1. Отменённый гол.
    2. Первая красная — только если фанаты разошлись в оценке судейства.
    3. Иначе пустая строка.
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
    """Контекст секции «ДНК матча» на странице матча.

    Все входные данные уже получены во вьюхе (агрегат, события, оценки, top/worst_players,
    fan_support). Возвращает None, если голосов нет, иначе dict: drama_index,
    drama_level, momentum_points, referee_divergence, hero, antihero,
    turning_point_text, consensus_level, consensus_text, fan_mood_text,
    controversial_episode.
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


# ---------------------------------------------------------------------------
# Короткие тексты и бейджи для карточек матчей в списках.
# Только из уже загруженных данных — безопасно рендерить пачками
# (оркестрация — matches/card_services.py).
# ---------------------------------------------------------------------------

# Минимум голосов для мини-ДНК на карточке.
CARD_DNA_MIN_VOTES = 3

# Минимум прогнозов для индекса сенсации.
SENSATION_MIN_PREDICTIONS = 5

# С какой минуты гол считается поздним.
LATE_GOAL_MINUTE_THRESHOLD = 75

# Пороги позиций для «битвы за топ» и «матча за выживание».
INTRIGUE_TOP_BATTLE_POSITION = 3
INTRIGUE_RELEGATION_ZONE_SIZE = 3
# С какой разницы мячей прошлой встречи подписываем «реванш».
INTRIGUE_REVENGE_MARGIN = 3

# Пороги бейджей по реакциям сообщества (явное большинство).
REACTION_BADGE_MIN_VOTES = 5
REACTION_BADGE_MIN_PCT = 40


def describe_intrigue(match, *, home_position=None, away_position=None, total_teams=None, last_meeting=None) -> str | None:
    """Тег интриги для карточки (один, по приоритету):
    1. Дерби (Team.rivals).
    2. Битва за топ-N.
    3. Матч за выживание.
    4. Реванш за крупное поражение в прошлой встрече.

    :param last_meeting: последняя очная встреча или None.
    :return: строка или None.
    """
    if getattr(match, 'is_derby', False):
        return 'Дерби'

    if (
        home_position is not None and away_position is not None
        and home_position <= INTRIGUE_TOP_BATTLE_POSITION and away_position <= INTRIGUE_TOP_BATTLE_POSITION
    ):
        return f'Битва за топ-{INTRIGUE_TOP_BATTLE_POSITION}'

    if home_position is not None and away_position is not None and total_teams:
        relegation_from = total_teams - INTRIGUE_RELEGATION_ZONE_SIZE + 1
        if home_position >= relegation_from and away_position >= relegation_from:
            return 'Матч за выживание'

    if last_meeting:
        home_score = last_meeting.get('home_score')
        away_score = last_meeting.get('away_score')
        if home_score is not None and away_score is not None and home_score != away_score:
            margin = abs(home_score - away_score)
            if margin >= INTRIGUE_REVENGE_MARGIN:
                # Называем проигравшую тогда команду.
                if home_score < away_score:
                    loser_team_id, loser_score, winner_score = last_meeting['home_team_id'], home_score, away_score
                else:
                    loser_team_id, loser_score, winner_score = last_meeting['away_team_id'], away_score, home_score
                loser_team = match.home_team if loser_team_id == match.home_team_id else match.away_team
                return f'Реванш {loser_team.name} за {loser_score}:{winner_score}'

    return None


def _parse_score(score_str: str | None) -> tuple[int, int] | None:
    """'2-1' -> (2, 1). None, если строка пустая или в другом формате."""
    if not score_str:
        return None
    parts = score_str.split('-')
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


def _score_outcome(home: int, away: int) -> str:
    if home > away:
        return 'home'
    if away > home:
        return 'away'
    return 'draw'


def _goal_changed_outcome(goals: list, last_goal) -> bool:
    """True, если гол изменил исход (победа/ничья/поражение) по score_after."""
    after = _parse_score(last_goal.score_after)
    if after is None:
        return False  # не можем проверить

    idx = goals.index(last_goal)
    before = (0, 0) if idx == 0 else _parse_score(goals[idx - 1].score_after)
    if before is None:
        return False

    return _score_outcome(*before) != _score_outcome(*after)


def describe_key_moment(match, events: list) -> str | None:
    """Главный момент завершённого матча:
    1. Поздний гол, реально изменивший исход.
    2. Красная карточка.
    3. Реализованный пенальти.
    4. Иначе None.

    :param events: список MatchEvent по минутам.
    """
    if match.decided_administratively or not events:
        return None

    goals = [e for e in events if e.event_type in ('goal', 'penalty', 'own_goal')]
    if goals:
        last_goal = goals[-1]
        if last_goal.minute >= LATE_GOAL_MINUTE_THRESHOLD and _goal_changed_outcome(goals, last_goal):
            who = f' ({last_goal.player})' if last_goal.player_id else ''
            return f'Гол на {last_goal.display_minute}\'{who} решил исход матча'

    red_cards = [e for e in events if e.event_type == 'red_card']
    if red_cards:
        event = red_cards[0]
        who = f' ({event.player})' if event.player_id else ''
        return f'Красная карточка на {event.display_minute}\'{who}'

    penalties = [e for e in events if e.event_type == 'penalty']
    if penalties:
        event = penalties[0]
        who = f' ({event.player})' if event.player_id else ''
        return f'Пенальти на {event.display_minute}\'{who}'

    return None


def describe_card_dna_traits(aggregate) -> list[str]:
    """Мини-ДНК на карточке: 0-3 фразы только из полей MatchAggregate, без запросов."""
    if aggregate is None or aggregate.total_votes < CARD_DNA_MIN_VOTES:
        return []

    traits = []
    level = _drama_level(aggregate.drama_index)
    if level == 'high':
        traits.append('Высокая драма')
    elif level == 'medium':
        # «Средняя драма», чтобы не путать с тегом интриги.
        traits.append('Умеренная драма')

    if 0 < aggregate.avg_fairness <= 5.0:
        traits.append('Жёсткая игра')

    if aggregate.turning_point_ratio >= TURNING_POINT_MIN_RATIO:
        traits.append('Был перелом')

    return traits[:3]


def compute_sensation_index(match, counts: dict | None, reaction_counts: dict | None = None) -> int | None:
    """Индекс сенсации 0-100: насколько уверенно сообщество ошиблось с фаворитом.

    Основной источник — прогнозы до матча, запасной — реакция «Неожиданно» после.
    None, если данных мало или фаворит победил.
    """
    if not counts or counts.get('total', 0) < SENSATION_MIN_PREDICTIONS:
        if (
            reaction_counts and reaction_counts.get('total', 0) >= REACTION_BADGE_MIN_VOTES
            and reaction_counts['upset_pct'] >= REACTION_BADGE_MIN_PCT
            and reaction_counts['upset'] >= reaction_counts['match_of_round']
            and reaction_counts['upset'] >= reaction_counts['boring']
        ):
            return reaction_counts['upset_pct']
        return None
    final_result = match.final_result
    if final_result is None:
        return None

    pct_by_choice = {'1': counts['home_pct'], 'X': counts['draw_pct'], '2': counts['away_pct']}
    favorite_choice = max(pct_by_choice, key=pct_by_choice.get)
    if favorite_choice == final_result:
        return None  # фаворит победил

    return round(pct_by_choice[favorite_choice])


def describe_reaction_badge(counts: dict | None) -> str | None:
    """Бейдж «Матч тура», если это явное большинство реакций."""
    if not counts or counts.get('total', 0) < REACTION_BADGE_MIN_VOTES:
        return None
    if (
        counts['match_of_round_pct'] >= REACTION_BADGE_MIN_PCT
        and counts['match_of_round'] >= counts['upset']
        and counts['match_of_round'] >= counts['boring']
    ):
        return 'Матч тура по мнению болельщиков'
    return None


def describe_table_impact(team, before_position: int | None, after_position: int | None) -> str | None:
    """Бейдж «Изменил таблицу». after_position — позиция сразу после этого матча."""
    if before_position is None or after_position is None or before_position == after_position:
        return None
    if after_position < before_position:
        return f'{team.name} поднялся на {after_position}-е место'
    return f'{team.name} опустился на {after_position}-е место'


def describe_finished_cta(has_hero: bool, has_dna: bool) -> dict:
    """Подпись CTA на карточке вместо «Голосование закрыто»."""
    if has_hero or has_dna:
        return {'icon': 'ti-chart-bar', 'label': 'Разобрать матч'}
    return {'icon': 'ti-star', 'label': 'Смотреть оценки игроков'}


# --- Реакции сообщества на завершённый матч ---

def submit_match_reaction(*, user, match, reaction: str):
    """Ставит/меняет реакцию пользователя. Только для завершённых матчей."""
    if match.status != 'finished':
        return None
    reaction_codes = dict(MatchReaction.REACTION_CHOICES)
    if reaction not in reaction_codes:
        return None
    obj, _created = MatchReaction.objects.update_or_create(
        match=match, user=user, defaults={'reaction': reaction},
    )
    return obj


def reaction_counts(match) -> dict:
    """Распределение реакций по одному матчу."""
    row = MatchReaction.objects.filter(match=match).aggregate(
        match_of_round=Count('id', filter=Q(reaction=MatchReaction.REACTION_MATCH_OF_ROUND)),
        upset=Count('id', filter=Q(reaction=MatchReaction.REACTION_UPSET)),
        boring=Count('id', filter=Q(reaction=MatchReaction.REACTION_BORING)),
    )
    total = row['match_of_round'] + row['upset'] + row['boring']

    def pct(n: int) -> int:
        return round(n * 100 / total) if total else 0

    return {
        'match_of_round': row['match_of_round'], 'upset': row['upset'], 'boring': row['boring'],
        'total': total,
        'match_of_round_pct': pct(row['match_of_round']),
        'upset_pct': pct(row['upset']),
        'boring_pct': pct(row['boring']),
    }


def user_match_reaction(user, match):
    if not user or not user.is_authenticated:
        return None
    return MatchReaction.objects.filter(match=match, user=user).first()


def bulk_reaction_data(matches, user) -> dict:
    """reaction_counts/user_match_reaction пачкой для списка матчей."""
    matches = list(matches)
    if not matches:
        return {}
    match_ids = [m.id for m in matches]

    counts_by_match = {
        m_id: {'match_of_round': 0, 'upset': 0, 'boring': 0, 'total': 0,
               'match_of_round_pct': 0, 'upset_pct': 0, 'boring_pct': 0}
        for m_id in match_ids
    }
    rows = (
        MatchReaction.objects.filter(match_id__in=match_ids)
        .values('match_id', 'reaction').annotate(n=Count('id'))
    )
    for row in rows:
        key = row['reaction']
        if key in counts_by_match[row['match_id']]:
            counts_by_match[row['match_id']][key] = row['n']
    for counts in counts_by_match.values():
        total = counts['match_of_round'] + counts['upset'] + counts['boring']
        counts['total'] = total
        for key in ('match_of_round', 'upset', 'boring'):
            counts[f'{key}_pct'] = round(counts[key] * 100 / total) if total else 0

    my_reactions = {}
    if user and user.is_authenticated:
        my_reactions = {
            r.match_id: r for r in MatchReaction.objects.filter(match_id__in=match_ids, user=user)
        }

    return {
        m_id: {'counts': counts_by_match[m_id], 'my_reaction': my_reactions.get(m_id)}
        for m_id in match_ids
    }


def top_reaction_matches(season, reaction: str, limit: int = 5, min_votes: int = REACTION_BADGE_MIN_VOTES) -> list:
    """Топ матчей сезона по реакции (пока не используется в UI).
    Сортировка по числу голосов, не по проценту.

    :param min_votes: минимум голосов за реакцию.
    """
    rows = (
        MatchReaction.objects.filter(match__season=season, reaction=reaction)
        .values('match_id').annotate(n=Count('id'))
        .filter(n__gte=min_votes).order_by('-n')[:limit]
    )
    match_ids = [row['match_id'] for row in rows]
    if not match_ids:
        return []
    matches_by_id = {
        m.id: m for m in Match.objects.filter(id__in=match_ids).select_related('home_team', 'away_team')
    }
    # Порядок по числу голосов.
    return [matches_by_id[m_id] for m_id in match_ids if m_id in matches_by_id]
