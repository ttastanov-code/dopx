# matches/card_services.py
"""
Bulk-оркестрация редизайна карточки матча (2026-09-10, прямая просьба
пользователя, полный бриф из 14 пунктов — "интрига, прогноз сообщества,
игрок матча DOPX, Match DNA, индекс сенсации и т.д., делай всё сразу").

ЕДИНАЯ ТОЧКА ВХОДА: `attach_card_extras(matches, request)` — вызывается ОДИН
раз на уже отпагинированную/срезанную страницу матчей (обычно ≤20 штук, см.
matches/views.py::MatchListView.paginate_by), навешивает новые атрибуты
`card_*` прямо на объекты `Match` — тот же приём, что уже применяется в
проекте для `list_prediction_counts`/`user_has_evaluated` (matches/views.py,
core/views.py): шаблон читает `match.card_intrigue` как обычное поле, без
кастомного dict-lookup фильтра в Django-шаблонах.

ЗАМЕНЯЕТ прежний дублированный код `bulk_prediction_data`/`user_has_evaluated`
в MatchListView И HomeView — обе вьюхи теперь вызывают эту одну функцию,
что заодно чинит скрытый пробел, найденный при подготовке этого редизайна:
HomeView никогда не вызывала `bulk_prediction_data`, поэтому инлайн-прогноз
1X2 на главной странице не показывался вообще, хотя на /matches/ работал.

СТОИМОСТЬ ЗАПРОСОВ (честно, не скрываем): большинство новых сигналов —
bulk-запросы (1 запрос на весь список, тот же принцип, что и у
bulk_prediction_data). Три штуки — H2H, форма команд и таблица "было/стало"
— считаются ПО ОДНОМУ запросу НА КАЖДЫЙ подходящий матч (H2H+форма — только
для scheduled/live, таблица "было" — только для finished), т.к. это
по-настоящему разные срезы данных для разных пар команд/разных моментов
времени, которые физически нельзя слить в один SQL-запрос без потери
точности. На типичной странице (≤20 матчей, из которых обычно куда меньше
одновременно scheduled+finished) это остаётся в разумных пределах — тот же
компромисс, что проект уже принимает в MatchDetailView (get_team_form
считает по 2 отдельных запроса на команду) и явно документирует как
осознанный выбор, а не забытую оптимизацию.
"""
from __future__ import annotations

from collections import defaultdict

from django.db.models import Count, Q

from aggregates.models import PlayerMatchAggregate
from aggregates.services import MIN_VOTES_FOR_DISPLAY
from evaluations.models import EvaluationSession
from events.models import MatchEvent
from matches.models import Match
from matches.services import (
    bulk_reaction_data,
    compute_sensation_index,
    describe_card_dna_traits,
    describe_finished_cta,
    describe_intrigue,
    describe_key_moment,
    describe_reaction_badge,
    describe_table_impact,
)
from predictions.services import bulk_final_prediction_counts, bulk_prediction_data
from teams.models import TeamSeasonStats
from teams.services import compute_standings_asof, describe_season_form_streak


def attach_card_extras(matches, request) -> None:
    """Точка входа — см. докстринг модуля. Мутирует объекты `matches` на
    месте, ничего не возвращает (тот же контракт, что и у прежнего inline-
    кода в MatchListView/HomeView, который этот вызов заменяет)."""
    matches = list(matches)
    if not matches:
        return

    user = request.user
    pre_match = [m for m in matches if m.status in ('scheduled', 'live')]
    finished = [m for m in matches if m.status == 'finished']

    _attach_evaluated_flag(matches, user)
    _attach_prediction_widget(matches, user)  # уже существовавшая логика, теперь общая для всех вьюх
    _attach_intrigue_and_pre_match(pre_match)
    _attach_personalization(matches, user)
    _attach_finished_extras(finished, user)


def _attach_evaluated_flag(matches, user) -> None:
    """user_has_evaluated — тот же bulk-запрос, что раньше был отдельно
    прописан в MatchListView.get_context_data и core/views.py::HomeView
    (под-набор recent_matches) — теперь один код на оба места."""
    if user.is_authenticated:
        evaluated_ids = set(
            EvaluationSession.objects.filter(
                user=user, match_id__in=[m.id for m in matches], status='completed',
            ).values_list('match_id', flat=True)
        )
        for match in matches:
            match.user_has_evaluated = match.id in evaluated_ids
    else:
        for match in matches:
            match.user_has_evaluated = False


def _attach_prediction_widget(matches, user) -> None:
    """list_prediction_counts/list_my_prediction (инлайн-прогноз 1X2) —
    перенесено сюда без изменения логики (см. предыдущее место в
    MatchListView.get_context_data), плюс пункт 5 брифа поверх готовых
    данных: короткий текст статуса вовлечённости ("Прогнозов: 247" / "Вы
    ещё не сделали прогноз") — НЕ новый запрос, просто новая строка из уже
    посчитанных counts/my_prediction."""
    prediction_data = bulk_prediction_data(matches, user)
    for match in matches:
        data = prediction_data.get(match.id)
        if not data:
            match.list_prediction_counts = None
            match.list_my_prediction = None
            match.card_engagement = None
            continue
        match.list_prediction_counts = data['counts']
        match.list_my_prediction = data['my_prediction']
        total = data['counts']['total']
        if data['my_prediction'] is not None:
            match.card_engagement = f'Вы сделали прогноз · Прогнозов: {total}' if total else 'Вы сделали прогноз'
        elif total:
            match.card_engagement = f'Прогнозов: {total}'
        else:
            match.card_engagement = 'Вы ещё не сделали прогноз' if user.is_authenticated else None


def _attach_personalization(matches, user) -> None:
    """Пункт 6 брифа — бейдж "Ваша команда", если пользователь подписан
    (`users.Follow`) на домашнюю и/или гостевую команду матча. Один bulk-
    запрос на всю страницу (тот же принцип, что и остальные bulk_* функции
    здесь) — прежде такого bulk-хелпера в проекте не было (см. отчёт
    ресёрча: единственный прецедент — точечный запрос в notifications/
    tasks.py, не для списка карточек)."""
    if not user.is_authenticated:
        for match in matches:
            match.card_followed_teams = []
        return

    from users.models import Follow

    team_ids = set()
    for match in matches:
        team_ids.add(match.home_team_id)
        team_ids.add(match.away_team_id)
    followed_ids = set(
        Follow.objects.filter(user=user, team_id__in=team_ids).values_list('team_id', flat=True)
    )
    for match in matches:
        names = []
        if match.home_team_id in followed_ids:
            names.append(match.home_team.name)
        if match.away_team_id in followed_ids:
            names.append(match.away_team.name)
        match.card_followed_teams = names


def _attach_intrigue_and_pre_match(pre_match) -> None:
    """Пункты 1, 3, 4 брифа — интрига/очный баланс/форма, только для
    ещё не сыгранных (scheduled/live) матчей на странице, см. докстринг
    модуля про стоимость запросов (по одному запросу H2H и по два запроса
    формы НА КАЖДЫЙ такой матч)."""
    if not pre_match:
        return

    season_ids = {m.season_id for m in pre_match}
    team_ids = set()
    for m in pre_match:
        team_ids.add(m.home_team_id)
        team_ids.add(m.away_team_id)

    positions = _bulk_current_positions(season_ids, team_ids)
    total_teams_by_season = _bulk_total_teams(season_ids)

    for match in pre_match:
        home_pos = positions.get((match.season_id, match.home_team_id))
        away_pos = positions.get((match.season_id, match.away_team_id))
        total_teams = total_teams_by_season.get(match.season_id)

        recent_meetings = _recent_meetings(match)  # один запрос, переиспользуется и для тега интриги, и для H2H
        last_meeting = recent_meetings[0] if recent_meetings else None
        match.card_intrigue = describe_intrigue(
            match, home_position=home_pos, away_position=away_pos,
            total_teams=total_teams, last_meeting=last_meeting,
        )
        match.card_h2h = _summarize_h2h(match, last_meeting_and_more=recent_meetings)

        # ИСПРАВЛЕНО (2026-09-11, прямая просьба пользователя — "стрик из
        # побед только в рамках 5 матчей пишем, а по факту в этом сезоне
        # серия длиннее"): раньше запрос был БЕЗ фильтра по сезону и с
        # срезом [:TEAM_FORM_RECENT_MATCHES] — серия физически не могла
        # превысить 5, даже если реальная серия в текущем сезоне длиннее
        # (или вообще началась в предыдущем сезоне, что тоже не то, что
        # нужно для "форма В ЭТОМ СЕЗОНЕ"). Теперь — все финишированные
        # матчи ИМЕННО текущего сезона команды (season=match.season), без
        # среза — describe_season_form_streak (teams/services.py) сама
        # находит, где серия реально обрывается.
        home_season_matches = list(
            Match.objects.filter(
                Q(home_team=match.home_team) | Q(away_team=match.home_team),
                season=match.season, status='finished', start_time__lt=match.start_time,
            ).select_related('home_team', 'away_team').order_by('-start_time')
        )
        away_season_matches = list(
            Match.objects.filter(
                Q(home_team=match.away_team) | Q(away_team=match.away_team),
                season=match.season, status='finished', start_time__lt=match.start_time,
            ).select_related('home_team', 'away_team').order_by('-start_time')
        )
        match.card_home_form_text = describe_season_form_streak(match.home_team, home_season_matches)
        match.card_away_form_text = describe_season_form_streak(match.away_team, away_season_matches)


def _bulk_current_positions(season_ids, team_ids) -> dict:
    rows = TeamSeasonStats.objects.filter(
        season_id__in=season_ids, team_id__in=team_ids,
    ).values('season_id', 'team_id', 'position')
    return {(r['season_id'], r['team_id']): r['position'] for r in rows if r['position'] is not None}


def _bulk_total_teams(season_ids) -> dict:
    from teams.models import TeamSeason

    rows = (
        TeamSeason.objects.filter(season_id__in=season_ids)
        .values('season_id')
        .annotate(n=Count('id'))
    )
    return {r['season_id']: r['n'] for r in rows}


def _recent_meetings(match, limit: int = 5) -> list:
    # ВАЖНО: .values(...) должен идти ДО среза [:limit] — Django запрещает
    # дальнейшую доработку queryset (filter/exclude/order_by/values и т.д.)
    # ПОСЛЕ того, как к нему уже применён срез (AssertionError "Cannot
    # filter a query once a slice has been taken"), см. аналогичный urgent-
    # фикс этой же природы в другом месте проекта. Порядок ниже — сначала
    # .values(), потом .order_by(), потом срез — синтаксически безопасен и
    # даёт тот же SQL (LIMIT применяется к уже спроецированным колонкам).
    return list(
        Match.objects.filter(
            Q(home_team=match.home_team, away_team=match.away_team)
            | Q(home_team=match.away_team, away_team=match.home_team),
            status='finished',
        ).exclude(id=match.id)
        .values('home_team_id', 'away_team_id', 'home_score', 'away_score', 'start_time')
        .order_by('-start_time')[:limit]
    )


def _summarize_h2h(match, last_meeting_and_more: list) -> dict | None:
    """Пункт 3 брифа — "Последние 5: Кайрат 3 · Ничьи 1 · Женис 1", всегда
    выражено через ИМЕНА команд ТЕКУЩЕГО матча, независимо от того, кто из
    них дома/в гостях играл в прошлых встречах."""
    if not last_meeting_and_more:
        return None
    home_wins = draws = away_wins = 0
    for row in last_meeting_and_more:
        home_score, away_score = row['home_score'], row['away_score']
        if home_score is None or away_score is None:
            continue
        # Приводим счёт каждой прошлой встречи к перспективе ДОМАШНЕЙ
        # команды ТЕКУЩЕГО матча (match.home_team), даже если тогда она
        # играла в гостях.
        if row['home_team_id'] == match.home_team_id:
            current_home_score, current_away_score = home_score, away_score
        else:
            current_home_score, current_away_score = away_score, home_score
        if current_home_score > current_away_score:
            home_wins += 1
        elif current_home_score < current_away_score:
            away_wins += 1
        else:
            draws += 1
    return {
        'count': len(last_meeting_and_more),
        'home_wins': home_wins, 'draws': draws, 'away_wins': away_wins,
    }


def _attach_finished_extras(finished, user) -> None:
    """Пункты 8, 9, 10, 11, 12, 13, 14 брифа — только для завершённых
    матчей на странице."""
    if not finished:
        return

    finished_ids = [m.id for m in finished]

    # --- 9: лучший игрок DOPX (bulk, один запрос) ---
    hero_rows = (
        PlayerMatchAggregate.objects.filter(
            match_id__in=finished_ids, total_votes__gte=MIN_VOTES_FOR_DISPLAY,
        ).select_related('player', 'player__team').order_by('match_id', '-performance_score')
    )
    hero_by_match = {}
    for row in hero_rows:
        hero_by_match.setdefault(row.match_id, row)  # первая строка группы = лучшая (см. order_by выше)

    # --- 8: главный момент (bulk fetch событий, группировка в Python) ---
    events_by_match = defaultdict(list)
    decided_admin_ids = {m.id for m in finished if m.decided_administratively}
    non_admin_ids = [match_id for match_id in finished_ids if match_id not in decided_admin_ids]
    if non_admin_ids:
        for event in (
            MatchEvent.objects.filter(match_id__in=non_admin_ids)
            .select_related('player').order_by('match_id', 'minute', 'added_time', 'id')
        ):
            events_by_match[event.match_id].append(event)

    # --- 11: реакции сообщества (bulk) ---
    reaction_data = bulk_reaction_data(finished, user)

    # --- 12: индекс сенсации (bulk распределение прогнозов) ---
    sensation_counts = bulk_final_prediction_counts(finished_ids)

    # --- 13: "изменил таблицу" — текущие позиции bulk'ом, "было" — по
    # запросу на матч (см. докстринг модуля про стоимость) ---
    season_ids = {m.season_id for m in finished}
    team_ids = set()
    for m in finished:
        team_ids.add(m.home_team_id)
        team_ids.add(m.away_team_id)
    current_positions = _bulk_current_positions(season_ids, team_ids)

    for match in finished:
        hero_row = hero_by_match.get(match.id)
        match.card_hero = {'player': hero_row.player, 'score': hero_row.performance_score} if hero_row else None

        events = events_by_match.get(match.id, [])
        match.card_key_moment = describe_key_moment(match, events)

        match.card_dna_traits = describe_card_dna_traits(getattr(match, 'aggregate', None))

        reaction_entry = reaction_data.get(match.id)
        match.card_reaction_counts = reaction_entry['counts'] if reaction_entry else None
        match.card_my_reaction = reaction_entry['my_reaction'] if reaction_entry else None
        # Доп. предложение (2026-09-10) — видимый бейдж "Матч тура", когда
        # сообщество явным большинством так и отметило (см. описание в
        # matches/services.py::describe_reaction_badge).
        match.card_reaction_badge = describe_reaction_badge(match.card_reaction_counts)

        # Реакции — запасной источник для индекса сенсации, если прогнозов
        # до матча было мало (см. докстринг compute_sensation_index).
        match.card_sensation = compute_sensation_index(
            match, sensation_counts.get(match.id), match.card_reaction_counts,
        )

        standings_before = compute_standings_asof(match.season, match.start_time)
        before_home = standings_before.get(match.home_team_id)
        before_away = standings_before.get(match.away_team_id)
        current_home = current_positions.get((match.season_id, match.home_team_id))
        current_away = current_positions.get((match.season_id, match.away_team_id))
        home_impact = describe_table_impact(match.home_team, before_home, current_home)
        away_impact = describe_table_impact(match.away_team, before_away, current_away)
        # Показываем максимум один факт — тот, где изменение позиций
        # больше (заметнее пользователю); при равенстве — домашняя команда.
        if home_impact and away_impact:
            home_delta = abs((before_home or 0) - (current_home or 0))
            away_delta = abs((before_away or 0) - (current_away or 0))
            match.card_table_impact = home_impact if home_delta >= away_delta else away_impact
        else:
            match.card_table_impact = home_impact or away_impact

        match.card_cta = describe_finished_cta(
            has_hero=match.card_hero is not None, has_dna=bool(match.card_dna_traits),
        )
