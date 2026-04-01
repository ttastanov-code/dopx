# leagues/views.py
from django.views.generic import ListView, DetailView
from django.db.models import Count, Avg, F, Q, Sum
from django.db.models.functions import Coalesce
from django.core.cache import cache  # ✅ Для кэширования
from leagues.models import League
from seasons.models import Season
from matches.models import Match
from teams.models import Team, TeamSeason
import logging

logger = logging.getLogger(__name__)


class LeagueListView(ListView):
    """Список всех лиг"""
    model = League
    template_name = 'leagues/list.html'
    context_object_name = 'leagues'
    paginate_by = 20
    
    def get_queryset(self):
        return League.objects.all().order_by('name')
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = 'Все лиги — DOPX'
        return context


class LeagueDetailView(DetailView):
    """Детальная страница лиги с турнирной таблицей"""
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
        
        # === 2. Активный сезон для таблицы ===
        active_season = seasons.filter(is_active=True).first()
        
        # === 3. Турнирная таблица (с кэшем и SQL агрегацией) ===
        standings = []  # ✅ По умолчанию пустой список
        
        if active_season:
            cache_key = f'league_{league.id}_season_{active_season.id}_standings'
            cached_standings = cache.get(cache_key)
            
            if cached_standings is not None:
                # ✅ Используем кэш
                standings = cached_standings
            else:
                # ✅ Инициализируем standings как список ПЕРЕД циклом
                standings = []  # ← 🔥 КЛЮЧЕВОЕ ИСПРАВЛЕНИЕ
                
                # Получаем все команды в сезоне
                teams_in_season = Team.objects.filter(
                    teamseason__season=active_season,
                    is_active=True
                ).distinct()
                
                for team in teams_in_season:
                    # ✅ ОДИН SQL запрос вместо 6+ (агрегация)
                    stats = Match.objects.filter(
                        season=active_season,
                        status='finished'
                    ).aggregate(
                        # Игры
                        home_played=Count('id', filter=Q(home_team=team)),
                        away_played=Count('id', filter=Q(away_team=team)),
                        
                        # Победы
                        home_wins=Count('id', filter=Q(home_team=team) & Q(home_score__gt=F('away_score'))),
                        away_wins=Count('id', filter=Q(away_team=team) & Q(away_score__gt=F('home_score'))),
                        
                        # Ничьи
                        home_draws=Count('id', filter=Q(home_team=team) & Q(home_score=F('away_score'))),
                        away_draws=Count('id', filter=Q(away_team=team) & Q(away_score=F('home_score'))),
                        
                        # ✅ Голы забитые — правильная обработка None
                        home_goals_scored=Sum('home_score', filter=Q(home_team=team)),
                        away_goals_scored=Sum('away_score', filter=Q(away_team=team)),
                        
                        # ✅ Голы пропущенные
                        home_goals_conceded=Sum('away_score', filter=Q(home_team=team)),
                        away_goals_conceded=Sum('home_score', filter=Q(away_team=team)),
                    )

                    # ✅ Пост-обработка: заменяем None на 0
                    played = (stats['home_played'] or 0) + (stats['away_played'] or 0)
                    wins = (stats['home_wins'] or 0) + (stats['away_wins'] or 0)
                    draws = (stats['home_draws'] or 0) + (stats['away_draws'] or 0)
                    losses = played - wins - draws

                    goals_scored = ((stats['home_goals_scored'] or 0) + 
                                    (stats['away_goals_scored'] or 0))
                    goals_conceded = ((stats['home_goals_conceded'] or 0) + 
                                    (stats['away_goals_conceded'] or 0))
                    goal_diff = goals_scored - goals_conceded
                    points = wins * 3 + draws
                    
                    standings.append({
                        'team_id': str(team.id),      # ← UUID как строка
                        'team_name': team.name,        # ← Только строка
                        'team_logo_url': team.logo_url,
                        'played': played,
                        'wins': wins,
                        'draws': draws,
                        'losses': losses,
                        'goals_scored': goals_scored,
                        'goals_conceded': goals_conceded,
                        'goal_diff': goal_diff,
                        'points': points,
                    })
                
                # Сортировка: очки → разница мячей → забитые голы
                standings.sort(key=lambda x: (-x['points'], -x['goal_diff'], -x['goals_scored']))
                
                # ✅ Сохраняем в кэш на 5 минут
                cache.set(cache_key, standings, 300)
        
        # === 4. Последние матчи ===
        recent_matches = Match.objects.filter(
            league=league,
            status='finished'  # 🔥 Добавлен фильтр
        ).select_related(
            'home_team', 'away_team', 'season', 'stadium'
        ).order_by('-start_time')[:10]
        
        context.update({
            'seasons': seasons,
            'active_season': active_season,
            'standings': standings,
            'recent_matches': recent_matches,
            'page_title': f'{league.name} — DOPX',
        })
        return context