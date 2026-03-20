# teams/views.py
from django.views.generic import ListView, DetailView
from django.db.models import Count, Avg, Sum, Q
from django.utils import timezone
from teams.models import Team, TeamSeason
from players.models import Player
from matches.models import Match
from aggregates.models import PlayerMatchAggregate, MatchAggregate
from evaluations.models import TeamEvaluation
from seasons.models import Season
import logging

logger = logging.getLogger(__name__)


# teams/views.py
class TeamListView(ListView):
    """Список всех команд"""
    model = Team
    template_name = 'teams/list.html'
    context_object_name = 'teams'
    paginate_by = 20

    def get_queryset(self):
        queryset = Team.objects.all()
        
        # Поиск по названию
        search = self.request.GET.get('q')
        if search:
            queryset = queryset.filter(name__icontains=search)
        
        # Фильтр по городу
        city = self.request.GET.get('city')
        if city:
            queryset = queryset.filter(city__icontains=city)
        
        # 🔥 FIX: Считаем ОБА типа матчей (дома + в гостях)
        queryset = queryset.annotate(
            home_matches_count=Count(
                'home_matches',
                filter=Q(home_matches__status='finished'),
                distinct=True
            ),
            away_matches_count=Count(
                'away_matches',
                filter=Q(away_matches__status='finished'),
                distinct=True
            )
        )
        
        return queryset.order_by('name')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = 'Все команды — DOPX'
        context['search_query'] = self.request.GET.get('q', '')
        context['cities'] = Team.objects.values_list('city', flat=True).distinct().exclude(city='')[:10]
        return context


class TeamDetailView(DetailView):
    """Детальная страница команды со статистикой"""
    model = Team
    template_name = 'teams/detail.html'
    context_object_name = 'team'

    def get_queryset(self):
        return Team.objects.all()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        team = self.object
        now = timezone.now()
        
        # 🔥 FIX: Получаем активный сезон для фильтрации
        current_season = Season.objects.filter(is_active=True).first()
        
        # Матчи команды
        home_matches = Match.objects.filter(home_team=team)
        away_matches = Match.objects.filter(away_team=team)
        
        # 🔥 FIX: Фильтруем матчи по сезону (если есть) и статусу
        if current_season:
            matches_filter = Q(
                Q(home_team=team) | Q(away_team=team),
                season=current_season,
                status='finished'
            )
        else:
            matches_filter = Q(
                Q(home_team=team) | Q(away_team=team),
                status='finished'
            )
        
        # Статистика матчей — ТОЛЬКО текущий сезон + finished
        total_matches = Match.objects.filter(matches_filter).count()

        wins = 0
        goals_scored = 0
        goals_conceded = 0

        for match in Match.objects.filter(matches_filter):
            if match.home_team == team and match.home_score and match.away_score:
                if match.home_score > match.away_score:
                    wins += 1
                goals_scored += match.home_score or 0
                goals_conceded += match.away_score or 0
            elif match.away_team == team and match.home_score and match.away_score:
                if match.away_score > match.home_score:
                    wins += 1
                goals_scored += match.away_score or 0
                goals_conceded += match.home_score or 0
        
        # Игроки команды
        players = Player.objects.filter(
            team=team, 
            is_active=True
        ).order_by('number')[:25]
        
        # Агрегаты игроков (топ 5)
        top_players = PlayerMatchAggregate.objects.filter(
            player__team=team
        ).select_related(
            'player', 
            'match'
        ).order_by('-performance_score')[:5]
        
        # Оценки команд (средние)
        team_evals = TeamEvaluation.objects.filter(
            team=team
        ).aggregate(
            avg_tactics=Avg('tactics'),
            avg_effort=Avg('effort'),
            avg_organization=Avg('organization'),
            avg_mentality=Avg('mentality'),
            total=Count('id'),
        )
        
        # Последние матчи — ТОЛЬКО текущий сезон + finished
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
            ).order_by('-start_time')[:10]
        else:
            recent_matches = Match.objects.filter(
                Q(home_team=team) | Q(away_team=team),
                status='finished'
            ).select_related(
                'home_team', 
                'away_team', 
                'league', 
                'season'
            ).order_by('-start_time')[:10]
        
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
        
        context.update({
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
            'page_title': f'{team.name} — DOPX',
        })
        return context