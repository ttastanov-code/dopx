# players/views.py
from django.views.generic import ListView, DetailView
from django.db.models import Avg, Count, Q, Sum
from django.utils import timezone
from players.models import Player
from teams.models import Team
from aggregates.models import PlayerMatchAggregate
from evaluations.models import PlayerEvaluation
from lineups.models import MatchLineupPlayer
import logging
import django.db.models as models

logger = logging.getLogger(__name__)

class PlayerListView(ListView):
    """Список всех игроков с поиском и фильтрами"""
    model = Player
    template_name = 'players/list.html'
    context_object_name = 'players'
    paginate_by = 20
    
    def get_queryset(self):
        # ✅ FIX: filter() ДО select_related(), и только для Player
        queryset = Player.objects.filter(is_active=True).select_related('team').prefetch_related(
            # ✅ Prefetch с ограничением: подгружаем только 1 лучший агрегат на игрока
            models.Prefetch(
                'match_aggregates',
                queryset=PlayerMatchAggregate.objects.order_by('-performance_score').only(
                    'id', 'performance_score', 'player_id'
                )[:1],
                to_attr='best_aggregate'  # ✅ Сохраняем в отдельный атрибут
            )
        ).annotate(
            # ✅ FIX: Считаем фактические матчи через lineup, а не агрегаты
            total_matches=Count(
                'matchlineupplayer__lineup__match',
                filter=Q(matchlineupplayer__lineup__match__status='finished'),
                distinct=True
            )
        )
        
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
        
        # 🔥 FIX: Считаем фактические матчи через MatchLineupPlayer
        from lineups.models import MatchLineupPlayer
        actual_matches_count = MatchLineupPlayer.objects.filter(
            player=player,
            lineup__match__status='finished'
        ).count()
        
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
        stats_raw = PlayerMatchAggregate.objects.filter(
            player=player
        ).aggregate(
            avg_performance=Avg('performance_score'),
            avg_risk=Avg('risk_index'),
            avg_maturity=Avg('maturity_score'),
            avg_potential=Avg('avg_potential'),
            evaluated_matches=Count('id', distinct=True),
            # ✅ ИСПРАВЛЕНО: было Count('total_votes') — это считало количество
            # СТРОК агрегата (у поля total_votes есть default=0, оно никогда
            # не NULL, поэтому Count всегда равнялся числу оценённых матчей,
            # а не реальному числу голосов). Нужна сумма голосов по матчам.
            total_votes=Sum('total_votes'),
        )

        # ✅ ИСПРАВЛЕНО: раньше при отсутствии оценок avg-поля тихо
        # заполнялись нулём и шаблон показывал "0" неотличимо от
        # реального низкого рейтинга (тот же класс бага, что и на
        # странице команды/главной). Теперь отдельно храним признак
        # has_evaluations, а сами avg-поля остаются None, если оценок
        # нет — шаблон показывает "—" вместо обманчивого нуля.
        has_evaluations = stats_raw['evaluated_matches'] > 0
        stats = {
            'avg_performance': round(stats_raw['avg_performance'], 2) if stats_raw['avg_performance'] is not None else None,
            'avg_risk': round(stats_raw['avg_risk'], 2) if stats_raw['avg_risk'] is not None else None,
            'avg_maturity': round(stats_raw['avg_maturity'], 2) if stats_raw['avg_maturity'] is not None else None,
            'avg_potential': round(stats_raw['avg_potential'], 2) if stats_raw['avg_potential'] is not None else None,
            'total_matches': actual_matches_count,  # реально сыгранные матчи (по составу)
            'evaluated_matches': stats_raw['evaluated_matches'] or 0,  # из них оценено болельщиками
            'total_votes': stats_raw['total_votes'] or 0,
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

        # НОВОЕ: ближайший сыгранный матч этого игрока, который ещё можно
        # оценить — используется для CTA в пустых состояниях ("История
        # выступлений" / "Лучшие матчи"), чтобы не просто прятать карточки,
        # а вести пользователя к действию, как на странице команды.
        recent_lineups = MatchLineupPlayer.objects.filter(
            player=player,
            lineup__match__status='finished'
        ).select_related('lineup__match').order_by('-lineup__match__start_time')[:5]
        votable_match = next(
            (lu.lineup.match for lu in recent_lineups if lu.lineup.match.is_voting_open()),
            None
        )

        context.update({
            'aggregates': aggregates,
            'stats': stats,
            'has_evaluations': has_evaluations,
            'best_matches': best_matches,
            'team': team,
            'votable_match': votable_match,
            'page_title': f'{player.first_name} {player.last_name} — DOPX',
        })
        return context