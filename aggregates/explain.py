# aggregates/explain.py
"""Объяснение рейтинга: из чего сложилась оценка игрока и как трибуны прожили матч."""
from __future__ import annotations

from collections import defaultdict
from statistics import mean

from django.db.models import Avg, Count, Q

# Меньше — реакции не показываем.
MIN_REACTIONS = 5
# Отличие от средней за сезон, заметное глазу.
FORM_DELTA = 0.3
FORM_MIN_MATCHES = 2
# Итог отличается от простого среднего голосов — объясняем почему.
SMOOTHING_GAP = 0.2
# Корзины распределения голосов (шкала 1-10).
BUCKETS = (('9–10', 9, 10), ('7–8', 7, 8), ('5–6', 5, 6), ('1–4', 1, 4))

KEY_EVENT_TYPES = ('goal', 'own_goal', 'penalty', 'red_card', 'disallowed_goal', 'var_check')
PLAYER_EVENT_TEXT = {
    'goal': ('ti-ball-football', 'Забил гол', 'up'),
    'penalty': ('ti-target-arrow', 'Забил с пенальти', 'up'),
    'own_goal': ('ti-ball-football', 'Автогол', 'down'),
    'red_card': ('ti-rectangle-vertical', 'Красная карточка', 'down'),
    'yellow_card': ('ti-rectangle-vertical', 'Жёлтая карточка', 'down'),
    'disallowed_goal': ('ti-ban', 'Гол отменили', 'neutral'),
}


def _fmt(value: float) -> str:
    """Запятая, как floatformat в шаблонах (ru)."""
    return f"{value:.1f}".replace(".", ",")


def _plural(n: int, forms: str) -> str:
    from core.templatetags.ui_extras import ru_plural

    return f"{n} {ru_plural(n, forms)}"


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


def vote_values(match_ids, player_ids) -> dict:
    """{(match_id, player_id): [оценки вклада]} — только засчитанные голоса, один запрос на матч."""
    from aggregates.services import countable_evaluations
    from evaluations.models import PlayerEvaluation

    result = defaultdict(list)
    for match_id in match_ids:
        qs = countable_evaluations(PlayerEvaluation.objects.filter(match_id=match_id, player_id__in=player_ids), match_id)
        for player_id, value in qs.values_list('player_id', 'contribution'):
            result[(match_id, player_id)].append(value)
    return result


def _distribution(values: list[int]) -> list[dict]:
    peak = max((sum(lo <= v <= hi for v in values) for _l, lo, hi in BUCKETS), default=0)
    rows = []
    for label, lo, hi in BUCKETS:
        count = sum(lo <= v <= hi for v in values)
        rows.append({'label': label, 'count': count, 'width': round(count * 100 / peak) if peak else 0})
    return rows


def explain_player(agg, *, votes=(), events=(), reactions=None, form=None, stat_rating=None, expert_take=None) -> dict:
    """Раскладка оценки: голоса и их разброс, свои/чужие/нейтральные, сглаживание, события, сравнение."""
    reactions = reactions or {}
    score = agg.performance_score
    votes = list(votes)
    why = {'score': score, 'headline': '', 'short': '', 'votes_n': len(votes), 'distribution': [], 'sides': [],
           'smoothing': '', 'factors': [], 'compare': []}

    if votes:
        high = sum(v >= 8 for v in votes)
        why['distribution'] = _distribution(votes)
        why['short'] = f"{high} из {len(votes)} поставили 8 и выше"
        why['headline'] = f"{_plural(len(votes), 'болельщик,болельщика,болельщиков')}, {high} из них поставили 8 и выше"
        plain = mean(votes)
        if abs(plain - score) >= SMOOTHING_GAP:
            why['smoothing'] = (
                f"Простое среднее голосов — {_fmt(plain)}. Итог {_fmt(score)}: "
                + ("голос фаната одной команды весит меньше, а мнение нейтральных — больше, "
                   "чтобы болельщики не задирали своих и не топили чужих."
                   if agg.own_fans_avg is not None or agg.rival_fans_avg is not None else
                   "голоса новичков и подозрительных аккаунтов весят меньше.")
            )

    for label, value in (('Болельщики его команды', agg.own_fans_avg), ('Болельщики соперника', agg.rival_fans_avg),
                         ('Нейтральные', getattr(agg, 'neutral_avg', None))):
        if value is not None:
            why['sides'].append({'label': label, 'value': value, 'width': round(value * 10)})

    # События: значимые раньше жёлтых.
    factors = []
    for event in events:
        if event.player_id == agg.player_id and event.event_type in PLAYER_EVENT_TEXT:
            icon, label, tone = PLAYER_EVENT_TEXT[event.event_type]
        elif event.assist_player_id == agg.player_id and event.event_type in ('goal', 'penalty'):
            icon, label, tone = 'ti-shoe', 'Отдал голевую передачу', 'up'
        else:
            continue
        text = f"{label} на {event.display_minute}'"
        stats = reactions.get(event.id)
        if stats and stats['total'] >= MIN_REACTIONS:
            text += f" — трибуны отреагировали {stats['total']} раз"
        factors.append((event.event_type == 'yellow_card', {'icon': icon, 'tone': tone, 'text': text}))
    why['factors'] = [f for _minor, f in sorted(factors, key=lambda pair: pair[0])]
    if expert_take:
        text = f"Ключевой игрок матча по мнению эксперта ({expert_take.display_name})"
        if expert_take.headline:
            text += f": «{expert_take.headline}»"
        why['factors'].append({'icon': 'ti-microphone', 'tone': 'neutral', 'text': text})

    if form and form[1] >= FORM_MIN_MATCHES:
        delta = score - form[0]
        if abs(delta) >= FORM_DELTA:
            text = f"Обычно в этом сезоне — {_fmt(form[0])}. Этот матч {'лучше' if delta > 0 else 'хуже'} на {_fmt(abs(delta))}."
        else:
            text = f"Обычно в этом сезоне — {_fmt(form[0])}. Матч на его привычном уровне."
        why['compare'].append(text)
    if stat_rating:
        why['compare'].append(f"По статистике матча — {_fmt(stat_rating)}.")

    why['has_details'] = bool(why['distribution'] or why['sides'] or why['factors'] or why['compare'])
    return why


def explain_players(match, aggs, *, events, stat_ratings=None, expert_takes=()) -> None:
    """Вешает agg.why на каждый агрегат; запросов — константа, а не на игрока."""
    aggs = list(aggs)
    if not aggs:
        return
    ids = [a.player_id for a in aggs]
    reactions = reaction_stats(events)
    form = _season_form(match, ids)
    votes = vote_values([match.id], ids)
    takes = {t.key_player_id: t for t in expert_takes if t.key_player_id}
    stat_ratings = stat_ratings or {}
    for agg in aggs:
        agg.why = explain_player(
            agg, votes=votes.get((match.id, agg.player_id), ()), events=events, reactions=reactions,
            form=form.get(agg.player_id), stat_rating=stat_ratings.get(agg.player_id),
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
    """История игрока: agg.why и agg.delta (к прошлому оценённому матчу). Запросов — на матч, не на причину."""
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
    rated_ids = [a.match_id for a in aggs if a.total_votes >= min_votes]
    votes = vote_values(rated_ids, [player.id])
    stat_ratings = stat_ratings or {}

    for agg in aggs:
        others = [s for m, s in season_rows.get(agg.match.season_id, []) if m != agg.match_id]
        agg.why = explain_player(
            agg, votes=votes.get((agg.match_id, player.id), ()), events=events_by_match.get(agg.match_id, []),
            reactions=reactions, form=(mean(others), len(others)) if others else None,
            stat_rating=stat_ratings.get(agg.match_id), expert_take=takes.get(agg.match_id),
        )

    # aggs — от новых к старым; дельта к предыдущему матчу с достаточным числом голосов.
    rated = [a for a in aggs if a.total_votes >= min_votes]
    for newer, older in zip(rated, rated[1:]):
        newer.delta = round(newer.performance_score - older.performance_score, 1)
