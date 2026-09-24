# teams/views.py
from datetime import timedelta

from django.db.models import Avg, Count, Exists, F, OuterRef, Q, Sum
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.decorators.clickjacking import xframe_options_exempt
from django.views.generic import ListView, DetailView
from django.utils import timezone
from core.utils import normalize_kz
from core.models import get_setting
from teams.models import Team, TeamSeason, TeamSeasonStats
from players.models import Player
from matches.models import Match
from aggregates.models import PlayerMatchAggregate, MatchAggregate, TeamMatchAggregate
from lineups.models import MatchLineupPlayer
from aggregates.services import MIN_VOTES_FOR_DISPLAY
from aggregates.services import vote_weighted_avg
from seasons.models import Season
import logging

logger = logging.getLogger(__name__)

# Сколько без матчей игрок ещё считается в составе (~15 месяцев).
ROSTER_STALE_THRESHOLD = timedelta(days=450)

class TeamListView(ListView):
    """Список команд."""
    model = Team
    template_name = 'teams/list.html'
    context_object_name = 'teams'
    paginate_by = 20

    def get_paginate_by(self, queryset):
        # Размер страницы — из настроек платформы.
        return get_setting("teams_list_page_size", self.paginate_by)

    def get_queryset(self):
        queryset = Team.objects.all()

        # По умолчанию — команды текущего сезона (TeamSeason). ?season=all — все.
        self.active_season = Season.get_primary_active()
        self.show_all = self.request.GET.get('season') == 'all'
        if self.active_season and not self.show_all:
            queryset = queryset.filter(teamseason__season=self.active_season)

        search = self.request.GET.get('q')
        if search:
            # Поиск через normalize_kz (Кайрат = Қайрат), фильтруем в Python.
            normalized_query = normalize_kz(search)
            matching_ids = [
                t.id for t in Team.objects.only('id', 'name')
                if normalized_query in normalize_kz(t.name)
            ]
            queryset = queryset.filter(id__in=matching_ids)
        # Матчи за текущий сезон; при ?season=all — за всю историю.
        home_season_q = Q(home_matches__season=self.active_season) if self.active_season and not self.show_all else Q()
        away_season_q = Q(away_matches__season=self.active_season) if self.active_season and not self.show_all else Q()
        queryset = queryset.annotate(
            home_matches_count=Count(
                'home_matches',
                filter=Q(home_matches__status='finished') & home_season_q,
                distinct=True
            ),
            away_matches_count=Count(
                'away_matches',
                filter=Q(away_matches__status='finished') & away_season_q,
                distinct=True
            )
        )
        return queryset.order_by('name')
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = 'Все команды — DOPX'
        context['search_query'] = self.request.GET.get('q', '')
        context['active_season'] = self.active_season
        context['show_all'] = self.show_all
        # Фильтра по городу нет — источник не присылает city для команд.
        return context

class TeamDetailView(DetailView):
    """Страница команды со статистикой за сезон."""
    model = Team
    template_name = 'teams/detail.html'
    context_object_name = 'team'
    
    def get_queryset(self):
        return Team.objects.all()
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        team = self.object
        now = timezone.now()
        
        # Текущий активный сезон
        active_season = Season.objects.filter(is_active=True).first()

        # Выбор сезона — только сезоны, где команда участвовала.
        team_seasons = Season.objects.filter(teamseason__team=team).distinct().order_by('-year')
        season_param = self.request.GET.get('season', '').strip()
        selected_season = team_seasons.filter(year=season_param).first() if season_param else None
        if selected_season is None:
            selected_season = active_season
        current_season = selected_season  # дальше по коду называется current_season

        # Матчи выбранного сезона, только завершённые
        if current_season:
            matches_filter = Q(
                Q(home_team=team) | Q(away_team=team),
                season=current_season,
                status='finished'
            )
        else:
            # Нет активного сезона — все завершённые
            matches_filter = Q(
                Q(home_team=team) | Q(away_team=team),
                status='finished'
            )
        
        # Статистика через aggregate
        stats_data = Match.objects.filter(matches_filter).aggregate(
            played=Count('id'),
            wins=Count('id', filter=(
                (Q(home_team=team) & Q(home_score__gt=F('away_score'))) |
                (Q(away_team=team) & Q(away_score__gt=F('home_score')))
            )),
            draws=Count('id', filter=(
                (Q(home_team=team) & Q(home_score=F('away_score'))) |
                (Q(away_team=team) & Q(away_score=F('home_score')))
            )),
            goals_scored=Sum(
                F('home_score'), filter=Q(home_team=team)
            ) + Sum(
                F('away_score'), filter=Q(away_team=team)
            ),
            goals_conceded=Sum(
                F('away_score'), filter=Q(home_team=team)
            ) + Sum(
                F('home_score'), filter=Q(away_team=team)
            ),
        )
        
        # NULL -> 0
        total_matches = stats_data['played'] or 0
        wins = stats_data['wins'] or 0
        goals_scored = (stats_data['goals_scored'] or 0)
        goals_conceded = (stats_data['goals_conceded'] or 0)
        
        logger.info(f"📊 Team {team.name} stats (season {current_season.year if current_season else 'N/A'}): "
                   f"Matches={total_matches}, Wins={wins}, Scored={goals_scored}, Conceded={goals_conceded}")
        
        # Состав команды за выбранный сезон.
        if current_season and not current_season.is_active:
            # Прошлый сезон — кто реально играл за команду в этом сезоне.
            players = Player.objects.filter(
                matchlineupplayer__lineup__team=team,
                matchlineupplayer__lineup__match__season=current_season,
            ).distinct().order_by('number')
        else:
            # Текущий сезон: игравшие за команду в этом сезоне
            # + игроки команды без единой записи в составах (новички).
            # Список полный, без среза.
            never_played_ids = Player.objects.filter(
                team=team, is_active=True, matchlineupplayer__isnull=True,
            ).values_list('id', flat=True)
            played_this_season_ids = []
            if current_season:
                played_this_season_ids = Player.objects.filter(
                    matchlineupplayer__lineup__team=team,
                    matchlineupplayer__lineup__match__season=current_season,
                ).values_list('id', flat=True)
            players = Player.objects.filter(
                Q(id__in=never_played_ids) | Q(id__in=played_this_season_ids)
            ).distinct().order_by('number')
        
        # Топ-5 игроков: матч засчитывается команде, за которую сыгран.
        played_for_this_team = MatchLineupPlayer.objects.filter(
            player_id=OuterRef('player_id'),
            lineup__team=team,
            lineup__match_id=OuterRef('match_id'),
        )
        top_players = PlayerMatchAggregate.objects.annotate(
            played_for_this_team=Exists(played_for_this_team)
        ).filter(
            played_for_this_team=True, total_votes__gte=MIN_VOTES_FOR_DISPLAY
        ).select_related(
            'player',
            'match'
        ).order_by('-performance_score')[:5]
        
        # Средние оценки команды по TeamMatchAggregate (взвешенные), total — сумма голосов.
        team_match_aggs = TeamMatchAggregate.objects.filter(team=team).aggregate(
            avg_tactics=vote_weighted_avg('avg_tactics'),
            avg_effort=vote_weighted_avg('avg_effort'),
            avg_organization=vote_weighted_avg('avg_organization'),
            avg_mentality=vote_weighted_avg('avg_mentality'),
            total=Sum('total_votes'),
        )
        team_evals = {
            'avg_tactics': team_match_aggs['avg_tactics'],
            'avg_effort': team_match_aggs['avg_effort'],
            'avg_organization': team_match_aggs['avg_organization'],
            'avg_mentality': team_match_aggs['avg_mentality'],
            'total': team_match_aggs['total'] or 0,
        }
        
        # Последние матчи текущего сезона. Лимит — из настроек платформы.
        recent_matches_limit = get_setting("team_recent_matches_limit", 10)
        if current_season:
            recent_matches = Match.objects.filter(
                Q(home_team=team) | Q(away_team=team),
                season=current_season,
                status='finished'
            ).select_related(
                'home_team',
                'away_team',
                'league',
                'season'
            ).order_by('-start_time')[:recent_matches_limit]
        else:
            recent_matches = Match.objects.filter(
                Q(home_team=team) | Q(away_team=team),
                status='finished'
            ).select_related(
                'home_team',
                'away_team',
                'league',
                'season'
            ).order_by('-start_time')[:recent_matches_limit]
        
        # Ближайшие матчи
        upcoming_matches = Match.objects.filter(
            Q(home_team=team) | Q(away_team=team),
            start_time__gte=now,
            status='scheduled'
        ).select_related(
            'home_team',
            'away_team',
            'league',
            'season'
        ).order_by('start_time')[:5]
        
        # Текущий сезон
        current_season_obj = Season.objects.filter(
            is_active=True,
            teamseason__team=team
        ).first()

        # Место в таблице из TeamSeasonStats; нет строк — досчитываем один раз.
        season_stats = None
        if current_season:
            if not TeamSeasonStats.objects.filter(season=current_season).exists():
                from aggregates.tasks import _recalculate_standings_for_season
                _recalculate_standings_for_season(current_season)
            season_stats = TeamSeasonStats.objects.filter(
                team=team, season=current_season
            ).first()
        total_teams_in_league = None
        if season_stats:
            total_teams_in_league = TeamSeasonStats.objects.filter(
                season=current_season
            ).count()

        # Матч команды, открытый для оценки (CTA).
        votable_match = next((m for m in recent_matches if m.is_voting_open), None)

        is_following = False
        if self.request.user.is_authenticated:
            from users.models import Follow
            is_following = Follow.objects.filter(user=self.request.user, team=team).exists()

        # Индекс настроения клуба: бейдж (сейчас) + график (за сезон).
        from teams.services import (
            compute_mood_trend, compute_mood_series, build_mood_chart,
            find_season_controversial_matches, get_team_form,
        )

        # Форма команды из recent_matches.
        team_form = get_team_form(team, recent_matches)

        mood_trend = compute_mood_trend(team)
        mood_series = compute_mood_series(team)
        # График настроения (teams/services.py::build_mood_chart).
        mood_chart = build_mood_chart(mood_series)
        controversial_matches = (
            find_season_controversial_matches(team, current_season) if current_season else []
        )

        context.update({
            'is_following': is_following,
            'mood_trend': mood_trend,
            'mood_chart': mood_chart,
            'team_form': team_form,
            'controversial_matches': controversial_matches,
            'total_matches': total_matches,
            'wins': wins,
            'goals_scored': goals_scored,
            'goals_conceded': goals_conceded,
            'players': players,
            'top_players': top_players,
            'team_evals': team_evals,
            'recent_matches': recent_matches,
            'upcoming_matches': upcoming_matches,
            'current_season': current_season_obj,
            'season_stats': season_stats,
            'total_teams_in_league': total_teams_in_league,
            'votable_match': votable_match,
            'team_seasons': team_seasons,
            'active_season': active_season,
            'selected_season': selected_season,
            'page_title': f'{team.name} — DOPX',
        })

        # Активная поправка рейтинга — значок «скорректировано».
        rating_correction = getattr(team, 'rating_correction', None)
        if rating_correction is not None and abs(rating_correction.correction) < 0.01:
            rating_correction = None
        context['rating_correction'] = rating_correction

        # Embed-код виджета.
        widget_url = self.request.build_absolute_uri(reverse('teams:widget', args=[team.id]))
        context['widget_embed_code'] = (
            f'<iframe src="{widget_url}" width="320" height="180" '
            f'style="border:none;border-radius:12px;overflow:hidden" '
            f'title="Рейтинг {team.name} на DOPX"></iframe>'
        )
        return context


@xframe_options_exempt
def team_rating_widget(request, pk):
    """Виджет команды для чужих сайтов. Рейтинг — среднее TeamMatchAggregate.performance_score.
    @xframe_options_exempt — страница read-only.
    """
    team = get_object_or_404(Team, pk=pk)

    evals_stats = TeamMatchAggregate.objects.filter(team=team).aggregate(
        avg_score=vote_weighted_avg('performance_score'), total_votes=Sum('total_votes'),
    )
    total_votes = evals_stats['total_votes'] or 0
    has_enough_votes = total_votes >= MIN_VOTES_FOR_DISPLAY

    from partners.services import track_widget_embed_view

    track_widget_embed_view(widget_type="team", entity_id=str(team.id), request=request)

    return render(request, 'widgets/team_rating.html', {
        'team': team,
        'avg_score': round(evals_stats['avg_score'], 1) if evals_stats['avg_score'] is not None else None,
        'total_votes': total_votes,
        'has_enough_votes': has_enough_votes,
    })