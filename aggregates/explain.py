# aggregates/explain.py
"""Объяснение рейтинга: почему у игрока такая оценка и как трибуны прожили матч.
Сводит оценки болельщиков, статистику, события с live-реакциями и мнение эксперта в короткие причины."""
from __future__ import annotations

from collections import defaultdict
from statistics import mean

from django.db.models import Avg, Count, Q

# Меньше — доля лайков случайна, не показываем.
MIN_REACTIONS = 5
# Отличие от средней за сезон, заметное глазу.
FORM_DELTA = 0.5
FORM_MIN_MATCHES = 2
# Свои и соперники: разошлись / сошлись.
SPLIT_GAP = 1.5
AGREE_GAP = 0.7
# Расхождение со статистикой Sportmonks.
STAT_GAP = 1.0
# Дрейф мнения: первые и последние трети голосов.
DRIFT_MIN_VOTES = 9
DRIFT_GAP = 0.7

KEY_EVENT_TYPES = ('goal', 'own_goal', 'penalty', 'red_card', 'disallowed_goal', 'var_check')
PLAYER_EVENT_TEXT = {
    'goal': ('ti-ball-football', 'Гол', 'up'),
    'penalty': ('ti-target-arrow', 'Гол с пенальти', 'up'),
    'own_goal': ('ti-ball-football', 'Автогол', 'down'),
    'red_card': ('ti-rectangle-vertical', 'Красная карточка', 'down'),
    'yellow_card': ('ti-rectangle-vertical', 'Жёлтая карточка', 'down'),
    'disallowed_goal': ('ti-ban', 'Отменённый гол', 'neutral'),
}


def _fmt(value: float) -> str:
    """Запятая, как floatformat в шаблонах (ru)."""
    return f"{value:.1f}".replace(".", ",")


def reaction_stats(events) -> dict:
    """{event_id: {'likes', 'dislikes', 'total', 'like_pct'}} одним запросом."""
    from events.models import EventReaction

    rows = (
        EventReaction.objects.filter(match_event__in=[e.id for e in events])
        .values('match_event_id')
        .annotate(likes=Count('id', filter=Q(reaction='like')), dislikes=Count('id', filter=Q(reaction='dislike')))
    )
    result = {}
    for row in rows:
        total = row['likes'] + row['dislikes']
        result[row['match_event_id']] = {
            'likes': row['likes'], 'dislikes': row['dislikes'], 'total': total,
            'like_pct': round(row['likes'] * 100 / total) if total else 0,
        }
    return result


def _reaction_note(stats: dict | None) -> str:
    """Громкость, а не доля 👍: за гол соперника чужие трибуны ставят 👎, процент вводит в заблуждение."""
    from core.templatetags.ui_extras import ru_plural

    if not stats or stats['total'] < MIN_REACTIONS:
        return ''
    return f"{stats['total']} {ru_plural(stats['total'], 'реакция,реакции,реакций')} трибун"


def _season_form(match, player_ids) -> dict:
    """{player_id: (средний рейтинг, матчей)} за сезон без этого матча."""
    from aggregates.models import PlayerMatchAggregate
    from aggregates.services import min_votes_for_display, published_q

    if not match.season_id or not player_ids:
        return {}
    rows = (
        PlayerMatchAggregate.objects.filter(
            published_q(), player_id__in=player_ids, match__season_id=match.season_id,
            total_votes__gte=min_votes_for_display(),
        )
        .exclude(match=match)
        .values('player_id')
        .annotate(avg=Avg('performance_score'), n=Count('id'))
    )
    return {r['player_id']: (r['avg'], r['n']) for r in rows}


def _vote_drift(match, player_ids) -> dict:
    """{player_id: (ранние, поздние)} — средний «вклад» первой и последней трети засчитанных голосов."""
    from aggregates.services import countable_evaluations
    from evaluations.models import PlayerEvaluation

    by_player = defaultdict(list)
    qs = countable_evaluations(PlayerEvaluation.objects.filter(match=match, player_id__in=player_ids), match.id)
    for player_id, value in qs.order_by('created_at').values_list('player_id', 'contribution'):
        by_player[player_id].append(value)
    result = {}
    for player_id, values in by_player.items():
        if len(values) < DRIFT_MIN_VOTES:
            continue
        third = len(values) // 3
        result[player_id] = (mean(values[:third]), mean(values[-third:]))
    return result


def explain_player(agg, *, events=(), reactions=None, form=None, drift=None, stat_rating=None, expert_take=None) -> dict:
    """{'headline', 'reasons': [{'icon', 'text', 'tone'}]} — tone: up / down / neutral."""
    reactions = reactions or {}
    reasons = []

    # События матча: гол, ассист, карточки — с реакцией трибун; значимые раньше жёлтых.
    event_reasons = []
    for event in events:
        if event.player_id == agg.player_id and event.event_type in PLAYER_EVENT_TEXT:
            icon, label, tone = PLAYER_EVENT_TEXT[event.event_type]
        elif event.assist_player_id == agg.player_id and event.event_type in ('goal', 'penalty'):
            icon, label, tone = 'ti-shoe', 'Голевая передача', 'up'
        else:
            continue
        note = _reaction_note(reactions.get(event.id))
        event_reasons.append((event.event_type == 'yellow_card', {
            'icon': icon, 'tone': tone, 'text': f"{label} на {event.display_minute}'" + (f" · {note}" if note else ''),
        }))
    reasons += [r for _minor, r in sorted(event_reasons, key=lambda pair: pair[0])]

    if form and form[1] >= FORM_MIN_MATCHES:
        delta = agg.performance_score - form[0]
        if abs(delta) >= FORM_DELTA:
            word = 'выше' if delta > 0 else 'ниже'
            reasons.append({'icon': 'ti-trending-up' if delta > 0 else 'ti-trending-down',
                            'tone': 'up' if delta > 0 else 'down',
                            'text': f"На {_fmt(abs(delta))} {word} обычного: в сезоне в среднем {_fmt(form[0])}"})
        else:
            reasons.append({'icon': 'ti-equal', 'tone': 'neutral',
                            'text': f"Свой обычный уровень: в сезоне в среднем {_fmt(form[0])}"})

    own, rival = agg.own_fans_avg, agg.rival_fans_avg
    if own is not None and rival is not None:
        gap = own - rival
        if abs(gap) >= SPLIT_GAP:
            reasons.append({'icon': 'ti-arrows-split', 'tone': 'neutral',
                            'text': f"Трибуны разошлись: {_fmt(own)} от своих против {_fmt(rival)} от соперников"})
        elif abs(gap) <= AGREE_GAP:
            reasons.append({'icon': 'ti-users-group', 'tone': 'up' if own >= 7 else 'neutral',
                            'text': f"Свои и соперники сошлись: {_fmt(own)} и {_fmt(rival)}"})

    if stat_rating:
        diff = stat_rating - agg.performance_score
        if diff >= STAT_GAP:
            text = f"Статистика ценит выше трибун: {_fmt(stat_rating)}"
        elif diff <= -STAT_GAP:
            text = f"Статистика скромнее трибун: {_fmt(stat_rating)}"
        else:
            text = f"Статистика согласна с трибунами: {_fmt(stat_rating)}"
        reasons.append({'icon': 'ti-chart-bar', 'tone': 'neutral', 'text': text})

    if drift:
        early, late = drift
        if abs(late - early) >= DRIFT_GAP:
            word = 'росла' if late > early else 'падала'
            reasons.append({'icon': 'ti-timeline', 'tone': 'up' if late > early else 'down',
                            'text': f"Оценка {word} по ходу голосования: с {_fmt(early)} у первых до {_fmt(late)} у поздних"})

    if expert_take:
        text = f"Ключевой игрок по мнению эксперта ({expert_take.display_name})"
        if expert_take.headline:
            text += f": «{expert_take.headline}»"
        reasons.append({'icon': 'ti-microphone', 'tone': 'neutral', 'text': text})

    return {'headline': reasons[0]['text'].split(' · ')[0] if reasons else '', 'reasons': reasons}


def explain_players(match, aggs, *, events, stat_ratings=None, expert_takes=()) -> None:
    """Вешает agg.why на каждый агрегат; запросов — константа, а не на игрока."""
    aggs = list(aggs)
    if not aggs:
        return
    ids = [a.player_id for a in aggs]
    reactions = reaction_stats(events)
    form = _season_form(match, ids)
    drift = _vote_drift(match, ids)
    takes = {t.key_player_id: t for t in expert_takes if t.key_player_id}
    stat_ratings = stat_ratings or {}
    for agg in aggs:
        agg.why = explain_player(
            agg, events=events, reactions=reactions, form=form.get(agg.player_id),
            drift=drift.get(agg.player_id), stat_rating=stat_ratings.get(agg.player_id),
            expert_take=takes.get(agg.player_id),
        )


def match_story(match, events, *, turning_points=()) -> dict | None:
    """«Как трибуны прожили матч»: ключевые события с live-реакцией и перелом по версии болельщиков.
    None — если рассказать нечего."""
    key_events = [e for e in events if e.event_type in KEY_EVENT_TYPES]
    if not key_events:
        return None
    reactions = reaction_stats(key_events)
    peak = max((reactions.get(e.id, {}).get('total', 0) for e in key_events), default=0)
    moments = []
    for event in key_events:
        stats = reactions.get(event.id)
        loud = bool(stats) and stats['total'] >= MIN_REACTIONS
        moments.append({
            'event': event,
            'side': event.team_side,
            'icon': PLAYER_EVENT_TEXT.get(event.event_type, ('ti-device-tv',))[0],
            'stats': stats if loud else None,
            'volume': round(stats['total'] * 100 / peak) if loud and peak else 0,
        })

    loudest = max((m for m in moments if m['stats']), key=lambda m: m['stats']['total'], default=None)
    divisive = min(
        (m for m in moments if m['stats'] and m['stats']['total'] >= MIN_REACTIONS * 2),
        key=lambda m: abs(m['stats']['like_pct'] - 50), default=None,
    )
    if divisive and abs(divisive['stats']['like_pct'] - 50) > 15:
        divisive = None

    voted = turning_points[0] if turning_points else None
    summary = []
    if voted:
        summary.append(f"Переломом болельщики назвали: {voted['label']} ({voted['pct']}% ответов).")
    if loudest:
        e = loudest['event']
        who = f" ({e.player_display_name})" if e.player_display_name else ''
        summary.append(f"Самый громкий момент: {e.get_event_type_display().lower()} на {e.display_minute}'{who}.")
    if divisive and divisive is not loudest:
        e = divisive['event']
        summary.append(f"Самый спорный: {e.get_event_type_display().lower()} на {e.display_minute}', "
                       f"трибуны разделились {divisive['stats']['like_pct']} на {100 - divisive['stats']['like_pct']}.")

    if not summary and not any(m['stats'] for m in moments):
        return None
    return {
        'moments': moments,
        'loudest_id': loudest['event'].id if loudest else None,
        'summary': summary,
    }


def explain_history(player, aggs, *, stat_ratings=None) -> None:
    """История игрока: agg.why и agg.delta (к прошлому оценённому матчу). Запросов — константа."""
    from aggregates.models import PlayerMatchAggregate
    from aggregates.services import min_votes_for_display, published_q
    from engagement.models import ExpertTake
    from events.models import MatchEvent

    aggs = list(aggs)
    if not aggs:
        return
    min_votes = min_votes_for_display()
    match_ids = [a.match_id for a in aggs]

    season_rows = defaultdict(list)
    for match_id, season_id, score in PlayerMatchAggregate.objects.filter(
        published_q(), player=player, total_votes__gte=min_votes,
        match__season_id__in={a.match.season_id for a in aggs},
    ).values_list('match_id', 'match__season_id', 'performance_score'):
        season_rows[season_id].append((match_id, score))

    events = list(
        MatchEvent.objects.filter(Q(player=player) | Q(assist_player=player), match_id__in=match_ids)
        .order_by('minute', 'added_time')
    )
    events_by_match = defaultdict(list)
    for event in events:
        events_by_match[event.match_id].append(event)
    reactions = reaction_stats(events)
    takes = {
        t.match_id: t for t in ExpertTake.objects.filter(is_published=True, key_player=player, match_id__in=match_ids)
        .select_related('expert')
    }
    stat_ratings = stat_ratings or {}

    for agg in aggs:
        others = [s for m, s in season_rows.get(agg.match.season_id, []) if m != agg.match_id]
        form = (mean(others), len(others)) if others else None
        agg.why = explain_player(
            agg, events=events_by_match.get(agg.match_id, []), reactions=reactions, form=form,
            stat_rating=stat_ratings.get(agg.match_id), expert_take=takes.get(agg.match_id),
        )

    # aggs — от новых к старым; дельта к предыдущему матчу с достаточным числом голосов.
    rated = [a for a in aggs if a.total_votes >= min_votes]
    for newer, older in zip(rated, rated[1:]):
        newer.delta = round(newer.performance_score - older.performance_score, 1)
