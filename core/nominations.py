# core/nominations.py
"""
Номинации сезона — витрина "интересных фактов" по реальным критериям
оценки болельщиков.

КОНТЕКСТ: на сайте уже собирается много узких критериев оценки —
качество решений судьи, самоотдача и организация команды, тактика и
работа с заменами тренера, потенциал и рискованность игрока, честность
матча — но ни один из них раньше нигде не был виден в виде "звания".
Пользователь видит их только заполняя визард, а после — данные просто
уходят в агрегаты. Этот модуль превращает сырые шкалы (1-10 / 0-100) в
понятные положительные и отрицательные номинации: кто лучший, а кто
антигерой сезона — ровно то, о чём попросили ("даже вот судья, кто
самый честный, а кто наоборот").

Используется на двух страницах:
- `core/views.py::HomeView` — без фильтра, по всей платформе.
- `leagues/views.py::LeagueDetailView` — с фильтром по конкретной лиге
  и активному сезону.

СТАТИСТИЧЕСКАЯ ЗАЩИТА: у каждой номинации порог `MIN_VOTES` — иначе
1-2 случайные оценки сделают "лучшим тренером" человека, которого
кто-то оценил один раз в шутку (тот же принцип, что уже применён к
best_attack/best_defense и "настроению сезона" на странице лиги).
Если ни один участник не набрал порог — номинация просто не попадает
в список: это витрина фактов, а не обязательный дашборд, который нужно
любой ценой заполнить.

Дублирование положительной/отрицательной пары одним и тем же
участником (например, единственный оценённый судья одновременно
оказывается и "лучшим", и "худшим" из-за MIN_VOTES=3 на пустой базе)
исключается явной проверкой в `_best_worst_pair`.
"""
from __future__ import annotations

from django.core.cache import cache
from django.db.models import Avg, Count, QuerySet

from evaluations.models import (
    CoachEvaluation,
    MatchEvaluation,
    PlayerEvaluation,
    RefereeEvaluation,
    TeamEvaluation,
)

MIN_VOTES = 3
CACHE_TTL = 300  # 5 минут — те же соображения, что и у остальной аналитики лиги


def _scope(qs: QuerySet, league, season) -> QuerySet:
    if league is not None:
        qs = qs.filter(match__league=league)
    if season is not None:
        qs = qs.filter(match__season=season)
    return qs


def _aggregate(qs: QuerySet, group_field: str, metric: str, extra_values: tuple[str, ...]):
    """Группирует по `group_field`, считает средний `metric` и число оценок."""
    values = (group_field,) + extra_values
    return (
        qs.exclude(**{f'{group_field}__isnull': True})
        .values(*values)
        .annotate(avg_value=Avg(metric), n=Count('id'))
        .filter(n__gte=MIN_VOTES)
    )


def _best_worst_pair(
    qs: QuerySet, group_field: str, metric: str, extra_values: tuple[str, ...] = (),
):
    """Возвращает (лучший, худший) по среднему `metric`, либо (X, None), если
    худший совпадает с лучшим (одна и та же запись не может быть одновременно
    в двух противоположных номинациях)."""
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
    """
    Собирает список номинаций. Каждый элемент:
    {key, title, subtitle, icon, sentiment ('positive'|'negative'|'neutral'),
     entity_kind, entity_url_name, entity_id, entity_name, entity_extra,
     value_label, votes}
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

    # --- Судьи: качество решений (decision_quality, 1-10) ---
    ref_qs = _scope(RefereeEvaluation.objects.all(), league, season)
    best_ref, worst_ref = _best_worst_pair(
        ref_qs, 'match__referee', 'decision_quality',
        ('match__referee__first_name', 'match__referee__last_name'),
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
            'entity_id': best_ref['match__referee'],
            'entity_name': f"{best_ref['match__referee__first_name']} {best_ref['match__referee__last_name']}",
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
            'entity_id': worst_ref['match__referee'],
            'entity_name': f"{worst_ref['match__referee__first_name']} {worst_ref['match__referee__last_name']}",
            'entity_extra': '',
            'value_label': f"{round(worst_ref['avg_value'], 1)}/10",
            'votes': worst_ref['n'],
        })

    # --- Судьи: влияние на исход матча (influence_score, 0-100) ---
    top_influence = _best_only(ref_qs, 'match__referee', 'influence_score',
                                ('match__referee__first_name', 'match__referee__last_name'))
    if top_influence:
        nominations.append({
            'key': 'influential_referee',
            'title': 'Главный герой матчей',
            'subtitle': 'Болельщики считают, что этот судья сильнее всех влияет на исход',
            'icon': 'ti-whistle',
            'sentiment': 'neutral',
            'entity_kind': 'referee',
            'entity_url_name': 'referees:detail',
            'entity_id': top_influence['match__referee'],
            'entity_name': f"{top_influence['match__referee__first_name']} {top_influence['match__referee__last_name']}",
            'entity_extra': '',
            'value_label': f"{round(top_influence['avg_value'])}/100",
            'votes': top_influence['n'],
        })

    # --- Команды: самоотдача (effort, 1-10) ---
    team_qs = _scope(TeamEvaluation.objects.all(), league, season)
    best_effort, worst_effort = _best_worst_pair(team_qs, 'team', 'effort', ('team__name',))
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

    # --- Команды: организация игры (organization, 1-10) ---
    best_org = _best_only(team_qs, 'team', 'organization', ('team__name',))
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

    # --- Тренеры: тактика (tactics, 1-10) ---
    coach_qs = _scope(CoachEvaluation.objects.all(), league, season)
    best_tactics = _best_only(coach_qs, 'coach', 'tactics',
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

    # --- Тренеры: работа с заменами (substitutions, 1-10) ---
    best_subs = _best_only(coach_qs, 'coach', 'substitutions',
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

    # --- Игроки: риск/нестабильность (risk, 1-10) ---
    player_qs = _scope(PlayerEvaluation.objects.all(), league, season)
    top_risk = _best_only(player_qs, 'player', 'risk',
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

    # --- Игроки: потенциал (potential, 1-10) ---
    top_potential = _best_only(player_qs, 'player', 'potential',
                                ('player__first_name', 'player__last_name'))
    if top_potential:
        nominations.append({
            'key': 'rising_talent',
            'title': 'Юное дарование',
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

    # --- Матчи: честность игры (fairness, 1-10) ---
    match_qs = _scope(MatchEvaluation.objects.all(), league, season)
    best_fair, worst_fair = _best_worst_pair(
        match_qs, 'match', 'fairness',
        ('match__home_team__name', 'match__away_team__name'),
    )
    if best_fair:
        nominations.append({
            'key': 'fair_match',
            'title': 'Самый честный матч',
            'subtitle': 'Матч с самой высокой оценкой честности игры',
            'icon': 'ti-heart-handshake',
            'sentiment': 'positive',
            'entity_kind': 'match',
            'entity_url_name': 'matches:detail',
            'entity_id': best_fair['match'],
            'entity_name': f"{best_fair['match__home_team__name']} — {best_fair['match__away_team__name']}",
            'entity_extra': '',
            'value_label': f"{round(best_fair['avg_value'], 1)}/10",
            'votes': best_fair['n'],
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
            'entity_id': worst_fair['match'],
            'entity_name': f"{worst_fair['match__home_team__name']} — {worst_fair['match__away_team__name']}",
            'entity_extra': '',
            'value_label': f"{round(worst_fair['avg_value'], 1)}/10",
            'votes': worst_fair['n'],
        })

    cache.set(cache_key, nominations, CACHE_TTL)
    return nominations
