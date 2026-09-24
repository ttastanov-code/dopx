# leagues/views.py
from django.views.generic import ListView, DetailView
from django.db.models import Count, Avg, F, Q, Sum
from django.db.models.functions import Coalesce
from django.core.cache import cache  # кэширование
from core.models import get_setting
from leagues.models import League
from seasons.models import Season
from matches.models import Match
from teams.models import Team, TeamSeason, TeamSeasonStats
from aggregates.models import MatchAggregate
from players.models import Player
from core.nominations import get_nominations
import logging

logger = logging.getLogger(__name__)


class LeagueListView(ListView):
    """Список лиг."""
    model = League
    template_name = 'leagues/list.html'
    context_object_name = 'leagues'
    paginate_by = 20

    def get_paginate_by(self, queryset):
        # Размер страницы — из настроек платформы.
        return get_setting("leagues_list_page_size", self.paginate_by)

    def get_queryset(self):
        return League.objects.all().order_by('name')
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = 'Все лиги — DOPX'
        return context


class LeagueDetailView(DetailView):
    """Страница лиги с турнирной таблицей."""
    model = League
    template_name = 'leagues/detail.html'
    context_object_name = 'league'
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        league = self.object
        
        # === 1. Сезоны ===
        seasons = Season.objects.filter(
            league=league
        ).annotate(
            match_count=Count('match')
        ).order_by('-year')
        
        # === 2. Активный и выбранный сезон ===
        active_season = seasons.filter(is_active=True).first()

        # ?season=<год> — выбранный сезон; неверный год — активный сезон, без 404.
        season_param = self.request.GET.get('season', '').strip()
        selected_season = None
        if season_param:
            selected_season = seasons.filter(year=season_param).first()
        if selected_season is None:
            selected_season = active_season

        # === 3. Турнирная таблица ===
        # Из TeamSeasonStats. Для старых сезонов без строк — досчитываем один раз.
        standings = []
        if selected_season:
            if not TeamSeasonStats.objects.filter(season=selected_season).exists():
                from aggregates.tasks import _recalculate_standings_for_season
                _recalculate_standings_for_season(selected_season)

            stats_rows = (
                TeamSeasonStats.objects.filter(season=selected_season)
                .select_related('team')
                .order_by('position', '-points', '-goal_diff', '-goals_scored')
            )
            standings = [
                {
                    'team_id': str(row.team_id),
                    'team_name': row.team.name,
                    'team_logo_url': row.team.logo_url,
                    'played': row.played,
                    'wins': row.wins,
                    'draws': row.draws,
                    'losses': row.losses,
                    'goals_scored': row.goals_scored,
                    'goals_conceded': row.goals_conceded,
                    'goal_diff': row.goal_diff,
                    'points': row.points,
                }
                for row in stats_rows
            ]

        # === 4. Последние матчи выбранного сезона ===
        recent_matches = Match.objects.filter(
            league=league,
            season=selected_season,
            status='finished'  # фильтр по сезону
        ).select_related(
            'home_team', 'away_team', 'season'
        ).order_by('-start_time')[:10] if selected_season else Match.objects.none()

        # === 5. Аналитика сезона ===
        top_scorers = []
        league_mood = None
        most_dramatic_match = None
        best_attack = None
        best_defense = None
        avg_goals_per_match = None

        if selected_season:
            cache_key = f'league_{league.id}_season_{selected_season.id}_analytics'
            cached = cache.get(cache_key)

            if cached is not None:
                top_scorers = cached['top_scorers']
                league_mood = cached['league_mood']
                most_dramatic_match = cached['most_dramatic_match']
                avg_goals_per_match = cached['avg_goals_per_match']
            else:
                # Бомбардиры — по событиям «гол».
                top_scorers = list(
                    Player.objects.filter(
                        events__match__league=league,
                        events__match__season=selected_season,
                        events__event_type='goal',
                    )
                    .select_related('team')
                    .annotate(goals=Count('events'))
                    .order_by('-goals')[:5]
                )

                # Настроение сезона — средние зрелищность/напряжение/драма по матчам с total_votes >= 3.
                MIN_VOTES_FOR_MOOD = 3
                mood_agg = MatchAggregate.objects.filter(
                    match__league=league,
                    match__season=selected_season,
                    total_votes__gte=MIN_VOTES_FOR_MOOD,
                ).aggregate(
                    avg_entertainment=Avg('avg_entertainment'),
                    avg_tension=Avg('avg_tension'),
                    avg_drama=Avg('drama_index'),
                )
                if mood_agg['avg_entertainment'] is not None:
                    league_mood = {
                        'avg_entertainment': round(mood_agg['avg_entertainment'], 1),
                        'avg_tension': round(mood_agg['avg_tension'], 1),
                        'avg_drama': round(mood_agg['avg_drama'], 1),
                    }

                # Самый драматичный матч сезона.
                dramatic = (
                    MatchAggregate.objects.filter(
                        match__league=league,
                        match__season=selected_season,
                        total_votes__gte=MIN_VOTES_FOR_MOOD,
                    )
                    .select_related('match__home_team', 'match__away_team')
                    .order_by('-drama_index')
                    .first()
                )
                if dramatic:
                    most_dramatic_match = dramatic

                # Голов за матч в среднем.
                goals_agg = Match.objects.filter(
                    league=league, season=selected_season, status='finished'
                ).aggregate(
                    total_home=Sum('home_score'),
                    total_away=Sum('away_score'),
                    matches_count=Count('id'),
                )
                if goals_agg['matches_count']:
                    total_goals = (goals_agg['total_home'] or 0) + (goals_agg['total_away'] or 0)
                    avg_goals_per_match = round(total_goals / goals_agg['matches_count'], 2)

                cache.set(cache_key, {
                    'top_scorers': top_scorers,
                    'league_mood': league_mood,
                    'most_dramatic_match': most_dramatic_match,
                    'avg_goals_per_match': avg_goals_per_match,
                }, 300)

            # Лучшая атака/защита из таблицы, минимум несколько сыгранных матчей.
            eligible = [row for row in standings if row['played'] >= 3]
            if eligible:
                best_attack = max(eligible, key=lambda r: r['goals_scored'])
                best_defense = min(eligible, key=lambda r: r['goals_conceded'])

        # === НОМИНАЦИИ СЕЗОНА ===
        # core/nominations.py с фильтром по лиге и выбранному сезону.
        nominations = get_nominations(league=league, season=selected_season) if selected_season else []

        context.update({
            'seasons': seasons,
            'active_season': active_season,
            'selected_season': selected_season,
            'standings': standings,
            'recent_matches': recent_matches,
            'top_scorers': top_scorers,
            'league_mood': league_mood,
            'most_dramatic_match': most_dramatic_match,
            'best_attack': best_attack,
            'best_defense': best_defense,
            'avg_goals_per_match': avg_goals_per_match,
            'nominations': nominations,
            'page_title': f'{league.name} — DOPX',
        })
        return context