# players/views.py
from django.views.generic import ListView, DetailView
from django.db.models import Avg, Count, Q
from django.utils import timezone
from players.models import Player
from teams.models import Team
from aggregates.models import PlayerMatchAggregate
from evaluations.models import PlayerEvaluation
import logging

logger = logging.getLogger(__name__)


class PlayerListView(ListView):
    """Список всех игроков с поиском и фильтрами"""
    model = Player
    template_name = 'players/list.html'
    context_object_name = 'players'
    paginate_by = 20

    def get_queryset(self):
        # ✅ FIX: filter() ДО select_related(), и только для Player
        queryset = Player.objects.filter(is_active=True).select_related('team')
        
        # Поиск по имени
        search = self.request.GET.get('q')
        if search:
            queryset = queryset.filter(
                Q(first_name__icontains=search) | 
                Q(last_name__icontains=search)
            )
        
        # Фильтр по команде
        team_id = self.request.GET.get('team')
        if team_id:
            queryset = queryset.filter(team_id=team_id)
        
        # Фильтр по позиции
        position = self.request.GET.get('position')
        if position:
            queryset = queryset.filter(position__icontains=position)
        
        return queryset.order_by('last_name', 'first_name')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = 'Все игроки — DOPX'
        context['search_query'] = self.request.GET.get('q', '')
        # ✅ FIX: Team не имеет is_active, поэтому просто берём все
        context['teams'] = Team.objects.all()[:20]
        context['positions'] = Player.objects.values_list('position', flat=True).distinct()[:10]
        return context


class PlayerDetailView(DetailView):
    """Детальная страница игрока со статистикой и историей оценок"""
    model = Player
    template_name = 'players/detail.html'
    context_object_name = 'player'

    def get_queryset(self):
        return Player.objects.select_related('team').all()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        player = self.object
        
        # Агрегаты игрока по матчам
        aggregates = PlayerMatchAggregate.objects.filter(
            player=player
        ).select_related(
            'match__league', 
            'match__season',
            'match__home_team',
            'match__away_team'
        ).order_by('-match__start_time')[:20]
        
        # Общая статистика
        stats = PlayerMatchAggregate.objects.filter(
            player=player
        ).aggregate(
            avg_performance=Avg('performance_score'),
            avg_risk=Avg('risk_index'),
            avg_maturity=Avg('maturity_score'),
            avg_potential=Avg('avg_potential'),
            total_matches=Count('id', distinct=True),
            total_votes=Count('total_votes'),
        )
        
        # Заполняем нулями если нет данных
        stats = {
            'avg_performance': round(stats['avg_performance'] or 0, 2),
            'avg_risk': round(stats['avg_risk'] or 0, 2),
            'avg_maturity': round(stats['avg_maturity'] or 0, 2),
            'avg_potential': round(stats['avg_potential'] or 0, 2),
            'total_matches': stats['total_matches'] or 0,
            'total_votes': stats['total_votes'] or 0,
        }
        
        # Лучшие матчи игрока
        best_matches = PlayerMatchAggregate.objects.filter(
            player=player
        ).select_related(
            'match__home_team',
            'match__away_team'
        ).order_by('-performance_score')[:5]
        
        # Команда игрока
        team = player.team
        
        context.update({
            'aggregates': aggregates,
            'stats': stats,
            'best_matches': best_matches,
            'team': team,
            'page_title': f'{player.first_name} {player.last_name} — DOPX',
        })
        return context