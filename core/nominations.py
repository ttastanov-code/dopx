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

2026-08-23, ЗАЩИТА ОТ СГОВОРА: до этой даты модуль считал номинации
напрямую по сырым `PlayerEvaluation`/`TeamEvaluation`/`CoachEvaluation`/
`RefereeEvaluation`/`MatchEvaluation` через `Avg()` без единой защиты —
пока весь остальной сайт (профили игроков/команд/тренеров/судей) уже
перешёл на взвешенные и винзоризованные агрегаты из `aggregates/services.py`,
эта витрина оставалась последней дырой: организованная группа могла
не суметь испортить рейтинг игрока в его профиле (там защита есть), но
могла бы выбить его в "антигерои сезона" на главной, если бы номинации
продолжали читать сырые оценки. Модуль переписан на чтение из
`aggregates.models.*MatchAggregate` — тех же таблиц, что показывают
профили сущностей: `avg_contribution`/`risk_index`/`avg_potential`,
`avg_tactics`/`avg_effort`/`avg_organization`/`avg_mentality`,
`avg_influence`/`avg_decision_quality`, `avg_fairness` там уже посчитаны
через `calculate_weighted_average` (вес голоса + винзоризация хвостов,
см. `aggregates/services.py`) при пересчёте агрегата матча — номинации
теперь наследуют ту же защиту автоматически, без дублирования логики.
"""
from __future__ import annotations

from django.core.cache import cache
from django.db.models import Avg, QuerySet, Sum

from aggregates.models import (
    CoachMatchAggregate,
    MatchAggregate,
    PlayerMatchAggregate,
    RefereeMatchAggregate,
    TeamMatchAggregate,
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
    """
    Группирует строки уже ПОСЧИТАННЫХ per-match агрегатов (взвешенных и
    винзоризованных, см. `aggregates/services.py`) по `group_field`,
    усредняет `metric` ПО МАТЧАМ и суммирует `total_votes` — это и есть
    порог статистической значимости `n`.

    `n = Sum('total_votes')`, а не `Count('id')` числа строк-агрегатов:
    один матч с 20 голосами не должен весить как один матч с 3 голосами
    при проверке `MIN_VOTES` — суммируем реальное число индивидуальных
    оценок, из которых эти строки посчитаны.
    """
    values = (group_field,) + extra_values
    return (
        qs.exclude(**{f'{group_field}__isnull': True})
        .values(*values)
        .annotate(avg_value=Avg(metric), n=Sum('total_votes'))
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

    # --- Судьи: качество решений (avg_decision_quality, 1-10) ---
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

    # --- Судьи: влияние на исход матча (avg_influence, 0-100) ---
    top_influence = _best_only(ref_qs, 'referee', 'avg_influence',
                                ('referee__first_name', 'referee__last_name'))
    if top_influence:
        nominations.append({
            'key': 'influential_referee',
            'title': 'Главный герой матчей',
            'subtitle': 'Болельщики считают, что этот судья сильнее всех влияет на исход',
            'icon': 'ti-whistle',
            'sentiment': 'neutral',
            'entity_kind': 'referee',
            'entity_url_name': 'referees:detail',
            'entity_id': top_influence['referee'],
            'entity_name': f"{top_influence['referee__first_name']} {top_influence['referee__last_name']}",
            'entity_extra': '',
            'value_label': f"{round(top_influence['avg_value'])}/100",
            'votes': top_influence['n'],
        })

    # --- Команды: самоотдача (avg_effort, 1-10) ---
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

    # --- Команды: организация игры (avg_organization, 1-10) ---
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

    # --- Тренеры: тактика (avg_tactics, 1-10) ---
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

    # --- Тренеры: работа с заменами (avg_substitutions, 1-10) ---
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

    # --- Игроки: риск/нестабильность (risk_index, 1-10) ---
    # risk_index, а не "сырое" avg_risk — risk_index уже утянут к
    # нейтральному якорю (apply_neutral_anchor, aggregates/services.py),
    # avg_risk остаётся незащищённым сырым средним. "Игрок на грани" —
    # единственная НЕГАТИВНАЯ персональная номинация на сайте, ей нужна
    # именно защищённая цифра.
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

    # --- Игроки: потенциал (avg_potential, 1-10) ---
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

    # --- Матчи: честность игры (avg_fairness, 1-10) ---
    # MatchAggregate — OneToOne с матчем (не группируем несколько строк на
    # одну сущность, как выше): каждая строка уже сама по себе один матч,
    # порог MIN_VOTES проверяем прямо на её total_votes.
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

    cache.set(cache_key, nominations, CACHE_TTL)
    return nominations
