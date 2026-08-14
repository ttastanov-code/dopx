# teams/views.py
from django.views.generic import ListView, DetailView
from django.db.models import Count, Avg, Sum, Q, F
from django.utils import timezone
from teams.models import Team, TeamSeason, TeamSeasonStats
from players.models import Player
from matches.models import Match
from aggregates.models import PlayerMatchAggregate, MatchAggregate
from evaluations.models import TeamEvaluation
from seasons.models import Season
import logging

logger = logging.getLogger(__name__)

class TeamListView(ListView):
    """Список всех команд"""
    model = Team
    template_name = 'teams/list.html'
    context_object_name = 'teams'
    paginate_by = 20
    
    def get_queryset(self):
        queryset = Team.objects.all()
        search = self.request.GET.get('q')
        if search:
            queryset = queryset.filter(name__icontains=search)
        city = self.request.GET.get('city')
        if city:
            queryset = queryset.filter(city__icontains=city)
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
    """✅ ИСПРАВЛЕНО: Детальная страница команды с правильной статистикой за ТЕКУЩИЙ СЕЗОН"""
    model = Team
    template_name = 'teams/detail.html'
    context_object_name = 'team'
    
    def get_queryset(self):
        return Team.objects.all()
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        team = self.object
        now = timezone.now()
        
        # ✅ ПОЛУЧАЕМ ТЕКУЩИЙ АКТИВНЫЙ СЕЗОН
        current_season = Season.objects.filter(is_active=True).first()
        
        # ✅ ФИЛЬТР МАТЧЕЙ: только текущий сезон + завершённые
        if current_season:
            matches_filter = Q(
                Q(home_team=team) | Q(away_team=team),
                season=current_season,
                status='finished'
            )
        else:
            # Если нет активного сезона, берём все завершённые
            matches_filter = Q(
                Q(home_team=team) | Q(away_team=team),
                status='finished'
            )
        
        # ✅ СТАТИСТИКА ЧЕРЕЗ AGGREGATE (эффективно и правильно)
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
        
        # ✅ ОБРАБОТКА NULL ЗНАЧЕНИЙ
        total_matches = stats_data['played'] or 0
        wins = stats_data['wins'] or 0
        goals_scored = (stats_data['goals_scored'] or 0)
        goals_conceded = (stats_data['goals_conceded'] or 0)
        
        logger.info(f"📊 Team {team.name} stats (season {current_season.year if current_season else 'N/A'}): "
                   f"Matches={total_matches}, Wins={wins}, Scored={goals_scored}, Conceded={goals_conceded}")
        
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

        # НОВОЕ: позиция в турнирной таблице текущего сезона — данные уже
        # считаются планово (aggregates/tasks.py::recalculate_season_standings
        # каждые 10 минут, см. комментарий в core/views.py), просто раньше
        # эта готовая строка нигде не использовалась на странице команды.
        season_stats = None
        if current_season:
            season_stats = TeamSeasonStats.objects.filter(
                team=team, season=current_season
            ).first()
        total_teams_in_league = None
        if season_stats:
            total_teams_in_league = TeamSeasonStats.objects.filter(
                season=current_season
            ).count()

        # Матч этой команды, который прямо сейчас можно оценить (для CTA
        # в пустом состоянии карточки "Оценки болельщиков") — НЕ просто
        # последний сыгранный, а именно тот, где voting_open_until ещё не
        # истёк, иначе кнопка вела бы на уже закрытое голосование.
        votable_match = next((m for m in recent_matches if m.is_voting_open), None)

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
            'season_stats': season_stats,
            'total_teams_in_league': total_teams_in_league,
            'votable_match': votable_match,
            'page_title': f'{team.name} — DOPX',
        })
        return context