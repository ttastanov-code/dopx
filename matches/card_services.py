# matches/card_services.py
"""Данные для карточек матчей.

attach_card_extras(matches, request) вызывается один раз на страницу и навешивает
на объекты Match атрибуты card_*, list_prediction_counts, user_has_evaluated.
Большинство сигналов — bulk-запросы; H2H, форма и влияние на таблицу —
по запросу на матч.
"""
from __future__ import annotations

from collections import defaultdict

from django.db.models import Count, Q
from django.utils import timezone

from aggregates.models import PlayerMatchAggregate
from aggregates.services import min_votes_for_display
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
from teams.services import compute_match_table_impact_positions, describe_season_form_streak


def attach_card_extras(matches, request) -> None:
    """Навешивает атрибуты на matches, ничего не возвращает."""
    matches = list(matches)
    if not matches:
        return

    user = request.user
    pre_match = [m for m in matches if m.status in ('scheduled', 'live')]
    finished = [m for m in matches if m.status == 'finished']

    _attach_evaluated_flag(matches, user)
    _attach_prediction_widget(matches, user)  # общая логика для всех вьюх
    _attach_intrigue_and_pre_match(pre_match)
    _attach_personalization(matches, user)
    _attach_finished_extras(finished, user)


def _attach_evaluated_flag(matches, user) -> None:
    """user_has_evaluated — одним запросом."""
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
    """Инлайн-прогноз 1X2 + текст вовлечённости из тех же данных."""
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
    """«Ваша команда» — подписки пользователя, одним запросом."""
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
    """Интрига, H2H, форма — для scheduled/live."""
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

        recent_meetings = _recent_meetings(match)  # один запрос для интриги и H2H
        last_meeting = recent_meetings[0] if recent_meetings else None
        match.card_intrigue = describe_intrigue(
            match, home_position=home_pos, away_position=away_pos,
            total_teams=total_teams, last_meeting=last_meeting,
        )
        match.card_h2h = _summarize_h2h(match, last_meeting_and_more=recent_meetings)

        # Серия — по всем матчам текущего сезона команды, без среза.
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
    # .values() до среза — после среза queryset менять нельзя.
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
    """«Последние 5: Кайрат 3 · Ничьи 1 · Женис 1» — по именам команд текущего матча."""
    if not last_meeting_and_more:
        return None
    home_wins = draws = away_wins = 0
    for row in last_meeting_and_more:
        home_score, away_score = row['home_score'], row['away_score']
        if home_score is None or away_score is None:
            continue
        # Счёт прошлых встреч — с точки зрения хозяев текущего матча.
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
    """Блоки завершённого матча."""
    if not finished:
        return

    finished_ids = [m.id for m in finished]

    # Рейтинги матча с идущим голосованием видит только тот, кто уже его оценил.
    now = timezone.now()
    evaluated_ids = set()
    if user is not None and user.is_authenticated:
        evaluated_ids = set(
            EvaluationSession.objects.filter(user=user, match_id__in=finished_ids, status='completed')
            .values_list('match_id', flat=True)
        )
    visible_ids = {m.id for m in finished if m.voting_open_until < now or m.id in evaluated_ids}

    # --- лучший игрок (bulk) ---
    hero_rows = (
        PlayerMatchAggregate.objects.filter(
            match_id__in=visible_ids, total_votes__gte=min_votes_for_display(),
        ).select_related('player', 'player__team').order_by('match_id', '-performance_score')
    )
    hero_by_match = {}
    for row in hero_rows:
        hero_by_match.setdefault(row.match_id, row)  # первая строка группы — лучшая

    # --- главный момент (bulk, группировка в Python) ---
    events_by_match = defaultdict(list)
    decided_admin_ids = {m.id for m in finished if m.decided_administratively}
    non_admin_ids = [match_id for match_id in finished_ids if match_id not in decided_admin_ids]
    if non_admin_ids:
        for event in (
            MatchEvent.objects.filter(match_id__in=non_admin_ids)
            .select_related('player').order_by('match_id', 'minute', 'added_time', 'id')
        ):
            events_by_match[event.match_id].append(event)

    # --- реакции (bulk) ---
    reaction_data = bulk_reaction_data(finished, user)

    # --- индекс сенсации (bulk) ---
    sensation_counts = bulk_final_prediction_counts(finished_ids)

    # --- изменение позиции в таблице: до и сразу после этого матча, запрос на матч ---
    for match in finished:
        hero_row = hero_by_match.get(match.id)
        match.card_hero = {'player': hero_row.player, 'score': hero_row.performance_score} if hero_row else None

        events = events_by_match.get(match.id, [])
        match.card_key_moment = describe_key_moment(match, events)

        match.card_dna_traits = (
            describe_card_dna_traits(getattr(match, 'aggregate', None)) if match.id in visible_ids else None
        )

        reaction_entry = reaction_data.get(match.id)
        match.card_reaction_counts = reaction_entry['counts'] if reaction_entry else None
        match.card_my_reaction = reaction_entry['my_reaction'] if reaction_entry else None
        # Бейдж «Матч тура» по реакциям.
        match.card_reaction_badge = describe_reaction_badge(match.card_reaction_counts)

        # Реакции — запасной источник сенсации при малом числе прогнозов.
        match.card_sensation = compute_sensation_index(
            match, sensation_counts.get(match.id), match.card_reaction_counts,
        )

        standings_before, standings_after = compute_match_table_impact_positions(match)
        before_home = standings_before.get(match.home_team_id)
        before_away = standings_before.get(match.away_team_id)
        after_home = standings_after.get(match.home_team_id)
        after_away = standings_after.get(match.away_team_id)
        home_impact = describe_table_impact(match.home_team, before_home, after_home)
        away_impact = describe_table_impact(match.away_team, before_away, after_away)
        # Показываем один факт — с большим сдвигом позиции; при равенстве — хозяева.
        if home_impact and away_impact:
            home_delta = abs((before_home or 0) - (after_home or 0))
            away_delta = abs((before_away or 0) - (after_away or 0))
            match.card_table_impact = home_impact if home_delta >= away_delta else away_impact
        else:
            match.card_table_impact = home_impact or away_impact

        match.card_cta = describe_finished_cta(
            has_hero=match.card_hero is not None, has_dna=bool(match.card_dna_traits),
        )
