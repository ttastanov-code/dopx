# core/nominations.py
"""Номинации сезона: лучшие и худшие по критериям оценки (судьи, команды, тренеры, игроки, матчи).
Используется на главной (вся платформа) и на странице лиги (активный сезон).
Читает per-match агрегаты: учитываются только матчи с порогом голосов, среднее — с весом
по голосам; кандидату нужно MIN_VOTES голосов и MIN_MATCHES таких матчей.
"""
from __future__ import annotations

from django.core.cache import cache
from django.db.models import Count, QuerySet, Sum

from aggregates.models import (
    CoachMatchAggregate,
    MatchAggregate,
    PlayerMatchAggregate,
    RefereeMatchAggregate,
    TeamMatchAggregate,
)
from aggregates.services import CONFIDENT_VOTES_THRESHOLD, min_votes_for_display, published_q, vote_weighted_avg

# Минимум голосов за кандидата суммарно — одна «спорная» игра не делает номинанта.
MIN_VOTES = CONFIDENT_VOTES_THRESHOLD
# Минимум матчей с достаточным числом голосов.
MIN_MATCHES = 3
CACHE_TTL = 300  # 5 минут


def _scope(qs: QuerySet, league, season) -> QuerySet:
    # Только матчи с закрытым голосованием и порогом голосов.
    qs = qs.filter(published_q(), total_votes__gte=min_votes_for_display())
    if league is not None:
        qs = qs.filter(match__league=league)
    if season is not None:
        qs = qs.filter(match__season=season)
    return qs


def _aggregate(qs: QuerySet, group_field: str, metric: str, extra_values: tuple[str, ...]):
    """Группирует агрегаты по group_field: среднее metric с весом по голосам,
    n = Sum(total_votes) — реальное число оценок, matches — число матчей.
    """
    values = (group_field,) + extra_values
    return (
        qs.exclude(**{f'{group_field}__isnull': True})
        .values(*values)
        .annotate(avg_value=vote_weighted_avg(metric), n=Sum('total_votes'), matches=Count('id'))
        .filter(n__gte=MIN_VOTES, matches__gte=MIN_MATCHES)
    )


def _best_worst_pair(
    qs: QuerySet, group_field: str, metric: str, extra_values: tuple[str, ...] = (),
):
    """(лучший, худший); худший = None, если совпадает с лучшим."""
    rows = _aggregate(qs, group_field, metric, extra_values)
    best = rows.order_by('-avg_value', '-n').first()
    worst = rows.order_by('avg_value', '-n').first()
    if best and worst and best[group_field] == worst[group_field]:
        worst = None
    return best, worst


def _best_only(qs: QuerySet, group_field: str, metric: str, extra_values: tuple[str, ...] = ()):
    rows = _aggregate(qs, group_field, metric, extra_values)
    return rows.order_by('-avg_value', '-n').first()


def get_nominations(*, league=None, season=None) -> list[dict]:
    """Список номинаций. Элемент:
    {key, title, subtitle, icon, sentiment, entity_kind, entity_url_name,
     entity_id, entity_name, entity_extra, value_label, value, scale, initials, images, votes}
    """
    if league is not None and season is not None:
        cache_key = f'nominations_league_{league.id}_season_{season.id}'
    elif league is None and season is None:
        cache_key = 'nominations_global'
    else:
        cache_key = f'nominations_league_{getattr(league, "id", "x")}_season_{getattr(season, "id", "x")}'

    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    nominations: list[dict] = []

    # --- Судьи: качество решений (1-10) ---
    ref_qs = _scope(RefereeMatchAggregate.objects.all(), league, season)
    best_ref, worst_ref = _best_worst_pair(
        ref_qs, 'referee', 'avg_decision_quality',
        ('referee__first_name', 'referee__last_name'),
    )
    if best_ref:
        nominations.append({
            'key': 'fair_referee',
            'title': 'Эталон судейства',
            'subtitle': 'Самое высокое качество решений по мнению болельщиков',
            'icon': 'ti-shield-check',
            'sentiment': 'positive',
            'entity_kind': 'referee',
            'entity_url_name': 'referees:detail',
            'entity_id': best_ref['referee'],
            'entity_name': f"{best_ref['referee__first_name']} {best_ref['referee__last_name']}",
            'entity_extra': '',
            'value_label': f"{round(best_ref['avg_value'], 1)}/10",
            'votes': best_ref['n'],
        })
    if worst_ref:
        nominations.append({
            'key': 'controversial_referee',
            'title': 'Спорные решения',
            'subtitle': 'Самое низкое качество решений по мнению болельщиков',
            'icon': 'ti-alert-triangle',
            'sentiment': 'negative',
            'entity_kind': 'referee',
            'entity_url_name': 'referees:detail',
            'entity_id': worst_ref['referee'],
            'entity_name': f"{worst_ref['referee__first_name']} {worst_ref['referee__last_name']}",
            'entity_extra': '',
            'value_label': f"{round(worst_ref['avg_value'], 1)}/10",
            'votes': worst_ref['n'],
        })

    # --- Судьи: влияние на исход (0-100) ---
    top_influence = _best_only(ref_qs, 'referee', 'avg_influence',
                                ('referee__first_name', 'referee__last_name'))
    if top_influence:
        nominations.append({
            'key': 'influential_referee',
            'title': 'Главный герой матчей',
            'subtitle': 'Сильнее всех влияет на исход матчей — хороший судья незаметен',
            'icon': 'ti-gavel',
            'sentiment': 'negative',
            'entity_kind': 'referee',
            'entity_url_name': 'referees:detail',
            'entity_id': top_influence['referee'],
            'entity_name': f"{top_influence['referee__first_name']} {top_influence['referee__last_name']}",
            'entity_extra': '',
            'value_label': f"{round(top_influence['avg_value'])}/100",
            'votes': top_influence['n'],
        })

    # --- Команды: самоотдача (1-10) ---
    team_qs = _scope(TeamMatchAggregate.objects.all(), league, season)
    best_effort, worst_effort = _best_worst_pair(team_qs, 'team', 'avg_effort', ('team__name',))
    if best_effort:
        nominations.append({
            'key': 'fighting_team',
            'title': 'Заряжены на борьбу',
            'subtitle': 'Команда с самой высокой оценкой самоотдачи',
            'icon': 'ti-bolt',
            'sentiment': 'positive',
            'entity_kind': 'team',
            'entity_url_name': 'teams:detail',
            'entity_id': best_effort['team'],
            'entity_name': best_effort['team__name'],
            'entity_extra': '',
            'value_label': f"{round(best_effort['avg_value'], 1)}/10",
            'votes': best_effort['n'],
        })
    if worst_effort:
        nominations.append({
            'key': 'passive_team',
            'title': 'Не хватает борьбы',
            'subtitle': 'Команда с самой низкой оценкой самоотдачи',
            'icon': 'ti-battery-1',
            'sentiment': 'negative',
            'entity_kind': 'team',
            'entity_url_name': 'teams:detail',
            'entity_id': worst_effort['team'],
            'entity_name': worst_effort['team__name'],
            'entity_extra': '',
            'value_label': f"{round(worst_effort['avg_value'], 1)}/10",
            'votes': worst_effort['n'],
        })

    # --- Команды: организация игры (1-10) ---
    best_org = _best_only(team_qs, 'team', 'avg_organization', ('team__name',))
    if best_org:
        nominations.append({
            'key': 'organized_team',
            'title': 'Железная организация',
            'subtitle': 'Команда с самой высокой оценкой командной организации',
            'icon': 'ti-puzzle',
            'sentiment': 'positive',
            'entity_kind': 'team',
            'entity_url_name': 'teams:detail',
            'entity_id': best_org['team'],
            'entity_name': best_org['team__name'],
            'entity_extra': '',
            'value_label': f"{round(best_org['avg_value'], 1)}/10",
            'votes': best_org['n'],
        })

    # --- Тренеры: тактика (1-10) ---
    coach_qs = _scope(CoachMatchAggregate.objects.all(), league, season)
    best_tactics = _best_only(coach_qs, 'coach', 'avg_tactics',
                               ('coach__first_name', 'coach__last_name'))
    if best_tactics:
        nominations.append({
            'key': 'tactical_coach',
            'title': 'Тактический гений',
            'subtitle': 'Тренер с самой высокой оценкой тактики',
            'icon': 'ti-chess-knight',
            'sentiment': 'positive',
            'entity_kind': 'coach',
            'entity_url_name': 'coaches:detail',
            'entity_id': best_tactics['coach'],
            'entity_name': f"{best_tactics['coach__first_name']} {best_tactics['coach__last_name']}",
            'entity_extra': '',
            'value_label': f"{round(best_tactics['avg_value'], 1)}/10",
            'votes': best_tactics['n'],
        })

    # --- Тренеры: замены (1-10) ---
    best_subs = _best_only(coach_qs, 'coach', 'avg_substitutions',
                            ('coach__first_name', 'coach__last_name'))
    if best_subs:
        nominations.append({
            'key': 'substitutions_master',
            'title': 'Мастер замен',
            'subtitle': 'Тренер с самой высокой оценкой работы со скамейкой запасных',
            'icon': 'ti-replace',
            'sentiment': 'positive',
            'entity_kind': 'coach',
            'entity_url_name': 'coaches:detail',
            'entity_id': best_subs['coach'],
            'entity_name': f"{best_subs['coach__first_name']} {best_subs['coach__last_name']}",
            'entity_extra': '',
            'value_label': f"{round(best_subs['avg_value'], 1)}/10",
            'votes': best_subs['n'],
        })

    # --- Игроки: риск (risk_index — с нейтральным якорем, 1-10) ---
    player_qs = _scope(PlayerMatchAggregate.objects.all(), league, season)
    top_risk = _best_only(player_qs, 'player', 'risk_index',
                           ('player__first_name', 'player__last_name'))
    if top_risk:
        nominations.append({
            'key': 'risky_player',
            'title': 'Игрок на грани',
            'subtitle': 'Самая высокая оценка риска и невынужденных ошибок',
            'icon': 'ti-dice-5',
            'sentiment': 'negative',
            'entity_kind': 'player',
            'entity_url_name': 'players:detail',
            'entity_id': top_risk['player'],
            'entity_name': f"{top_risk['player__first_name']} {top_risk['player__last_name']}",
            'entity_extra': '',
            'value_label': f"{round(top_risk['avg_value'], 1)}/10",
            'votes': top_risk['n'],
        })

    # --- Игроки: потенциал (1-10) ---
    top_potential = _best_only(player_qs, 'player', 'avg_potential',
                                ('player__first_name', 'player__last_name'))
    if top_potential:
        nominations.append({
            'key': 'rising_talent',
            'title': 'Скрытый потенциал',
            'subtitle': 'Самая высокая оценка потенциала роста',
            'icon': 'ti-rocket',
            'sentiment': 'positive',
            'entity_kind': 'player',
            'entity_url_name': 'players:detail',
            'entity_id': top_potential['player'],
            'entity_name': f"{top_potential['player__first_name']} {top_potential['player__last_name']}",
            'entity_extra': '',
            'value_label': f"{round(top_potential['avg_value'], 1)}/10",
            'votes': top_potential['n'],
        })

    # --- Матчи: честность игры (1-10) ---
    # MatchAggregate — одна строка на матч: нужен MIN_VOTES голосов за сам матч.
    match_agg_qs = (
        _scope(MatchAggregate.objects.all(), league, season)
        .filter(total_votes__gte=MIN_VOTES)
        .select_related('match__home_team', 'match__away_team')
    )
    best_fair = match_agg_qs.order_by('-avg_fairness', '-total_votes').first()
    worst_fair = match_agg_qs.order_by('avg_fairness', '-total_votes').first()
    if best_fair and worst_fair and best_fair.match_id == worst_fair.match_id:
        worst_fair = None
    if best_fair:
        nominations.append({
            'key': 'fair_match',
            'title': 'Самый честный матч',
            'subtitle': 'Матч с самой высокой оценкой честности игры',
            'icon': 'ti-heart-handshake',
            'sentiment': 'positive',
            'entity_kind': 'match',
            'entity_url_name': 'matches:detail',
            'entity_id': best_fair.match_id,
            'entity_name': f"{best_fair.match.home_team.name} — {best_fair.match.away_team.name}",
            'entity_extra': '',
            'value_label': f"{round(best_fair.avg_fairness, 1)}/10",
            'votes': best_fair.total_votes,
        })
    if worst_fair:
        nominations.append({
            'key': 'controversial_match',
            'title': 'Самый скандальный матч',
            'subtitle': 'Матч с самой низкой оценкой честности игры',
            'icon': 'ti-swords',
            'sentiment': 'negative',
            'entity_kind': 'match',
            'entity_url_name': 'matches:detail',
            'entity_id': worst_fair.match_id,
            'entity_name': f"{worst_fair.match.home_team.name} — {worst_fair.match.away_team.name}",
            'entity_extra': '',
            'value_label': f"{round(worst_fair.avg_fairness, 1)}/10",
            'votes': worst_fair.total_votes,
        })

    # Число и шкала отдельно («8.4/10» -> 8.4, 10) — для крупной цифры в карточке.
    for nom in nominations:
        nom['value'], nom['scale'] = nom['value_label'].split('/')
        nom['initials'] = ''.join(word[0] for word in nom['entity_name'].split()[:2] if word[:1].isalnum()).upper()
    _attach_images(nominations)

    cache.set(cache_key, nominations, CACHE_TTL)
    return nominations


def _attach_images(nominations: list[dict]) -> None:
    """images — URL логотипов/фото (у матча — оба клуба), пустой список, если картинок нет."""
    from coaches.models import Coach
    from matches.models import Match
    from players.models import Player
    from referees.models import Referee
    from teams.models import Team

    def photo(obj):
        return obj.photo_display if obj else None

    ids: dict[str, set] = {}
    for nom in nominations:
        ids.setdefault(nom['entity_kind'], set()).add(nom['entity_id'])
    teams = Team.objects.in_bulk(ids.get('team', ()))
    players = Player.objects.in_bulk(ids.get('player', ()))
    coaches = Coach.objects.in_bulk(ids.get('coach', ()))
    referees = Referee.objects.in_bulk(ids.get('referee', ()))
    matches = Match.objects.select_related('home_team', 'away_team').in_bulk(ids.get('match', ()))

    for nom in nominations:
        kind, pk = nom['entity_kind'], nom['entity_id']
        if kind == 'team':
            urls = [teams[pk].logo_display if pk in teams else None]
        elif kind == 'match' and pk in matches:
            urls = [matches[pk].home_team.logo_display, matches[pk].away_team.logo_display]
        else:
            source = {'player': players, 'coach': coaches, 'referee': referees}.get(kind, {})
            urls = [photo(source.get(pk))]
        nom['images'] = [u for u in urls if u]


# Порядок и подписи категорий в блоке номинаций.
CATEGORIES = (
    ('referee', 'Судьи', 'ti-cards'),
    ('team', 'Команды', 'ti-shield'),
    ('coach', 'Тренеры', 'ti-clipboard-list'),
    ('player', 'Игроки', 'ti-shirt-sport'),
    ('match', 'Матчи', 'ti-ball-football'),
)


def group_nominations(nominations: list[dict]) -> list[dict]:
    """Номинации по категориям: внутри сначала лучшие, потом антирекорды."""
    groups = []
    for kind, title, icon in CATEGORIES:
        items = [n for n in nominations if n['entity_kind'] == kind]
        if items:
            items.sort(key=lambda n: n['sentiment'] == 'negative')
            groups.append({'kind': kind, 'title': title, 'icon': icon, 'items': items})
    return groups
