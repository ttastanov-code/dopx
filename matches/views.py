# matches/views.py
from django.shortcuts import render, get_object_or_404
from django.views.generic import ListView, DetailView
from django.utils import timezone
from django.db.models import Avg, Count, Q, Prefetch, Sum, F, Value, Case, When, DateTimeField, DurationField
from matches.models import Match
from aggregates.models import MatchAggregate, PlayerMatchAggregate
from evaluations.models import TeamEvaluation, PlayerEvaluation, MatchEvaluation, ContextEvaluation, EvaluationSession
from lineups.models import MatchLineup
from seasons.models import Season
from leagues.models import League
import logging
from django.views.decorators.http import require_http_methods

logger = logging.getLogger(__name__)

class MatchListView(ListView):
    """Список всех матчей с фильтрами"""
    model = Match
    template_name = 'matches/list.html'
    context_object_name = 'matches'
    paginate_by = 20
    
    def get_queryset(self):
        queryset = Match.objects.select_related(
            'home_team',
            'away_team',
            'league',
            'season',
            'stadium'
        ).prefetch_related(
            'aggregate',
            # ✅ для match.is_derby (matches/models.py) — без этого N+1 на
            # каждой карточке списка.
            'home_team__rivals',
        )
        
        # Фильтр по статусу
        status = self.request.GET.get('status')
        
        if status == 'scheduled':
            # Запланированные - от начала года до конца
            queryset = queryset.filter(status='scheduled').order_by('start_time')
        elif status == 'live':
            # Live - сначала матчи, которые раньше начались
            queryset = queryset.filter(status='live').order_by('start_time')
        elif status == 'finished':
            # Завершенные - ближе к сегодняшнему дню сначала
            queryset = queryset.filter(status='finished').order_by('-start_time')
        else:
            # ИСПРАВЛЕНО: раньше здесь была сортировка по возрастанию даты
            # (`order_by('start_time')`) — на базе из пары сотен матчей
            # пользователю приходилось листать много страниц старых
            # завершённых матчей, прежде чем добраться до сегодняшних/
            # ближайших. Теперь сортируем по БЛИЗОСТИ к текущему моменту:
            # только что прошедшие, live и ближайшие предстоящие матчи
            # всегда наверху, вне зависимости от того, сколько всего
            # матчей накопилось в базе.
            # ИСПРАВЛЕНИЕ БАГА: первая версия использовала Abs() поверх
            # интервала — Postgres не имеет функции abs(interval), падало
            # с ProgrammingError. Тот же результат (расстояние до "сейчас")
            # без ABS: CASE — для будущих матчей берём (start_time - now)
            # положительным (сортировка по возрастанию = ближайшие сначала),
            # для прошедших — (now - start_time), тоже положительным
            # (сортировка по возрастанию = самые недавние сначала). Обе
            # ветки используют только вычитание timestamp - timestamp,
            # которое Postgres поддерживает нативно.
            now = Value(timezone.now(), output_field=DateTimeField())
            queryset = queryset.annotate(
                time_distance=Case(
                    When(start_time__gte=timezone.now(), then=F('start_time') - now),
                    default=now - F('start_time'),
                    output_field=DurationField(),
                )
            ).order_by('time_distance')
        
        # Фильтр по лиге
        league_id = self.request.GET.get('league')
        if league_id:
            queryset = queryset.filter(league_id=league_id)
        
        # Фильтр по сезону
        season_id = self.request.GET.get('season')
        if season_id:
            queryset = queryset.filter(season_id=season_id)
        
        return queryset
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = 'Все матчи — DOPX'
        context['current_status'] = self.request.GET.get('status', '')
        context['current_league'] = self.request.GET.get('league', '')
        context['current_season'] = self.request.GET.get('season', '')
        context['leagues'] = League.objects.all()[:10]
        context['seasons'] = Season.objects.filter(is_active=True)[:5]
        context['now'] = timezone.now()
        return context

class MatchDetailView(DetailView):
    """Детальная страница матча + результаты оценок"""
    model = Match
    template_name = 'matches/detail.html'
    context_object_name = 'match'
    
    def get_queryset(self):
        return Match.objects.select_related(
            'home_team',
            'away_team',
            'league',
            'season',
            'home_coach',
            'away_coach',
            'referee',
            'stadium'
        ).prefetch_related(
            'lineups__players__player',
            'lineups__players__player__team',
            'aggregate',
            'player_aggregates__player',
            'player_aggregates__player__team',
            'events',
            'coach_aggregates__coach',
            'home_team__rivals',  # ✅ для match.is_derby
        )
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        match = self.object
        now = timezone.now()
        
        # Проверка: голосование открыто?
        voting_open = (
            match.voting_open_until > now and
            match.status == 'finished'
        )
        
        # Проверка: пользователь уже оценил этот матч?
        user_has_evaluated = False
        if self.request.user.is_authenticated:
            user_has_evaluated = EvaluationSession.objects.filter(
                user=self.request.user,
                match=match,
                status='completed'
            ).exists()
        
        # Агрегаты матча
        match_agg = getattr(match, 'aggregate', None)
        
        # Топ 5 игроков матча
        top_players = PlayerMatchAggregate.objects.filter(
            match=match
        ).select_related(
            'player',
            'player__team'
        ).order_by('-performance_score')[:5]
        
        # Худшие 3 игрока матча
        worst_players = PlayerMatchAggregate.objects.filter(
            match=match
        ).select_related(
            'player',
            'player__team'
        ).order_by('performance_score')[:3]
        
        # Оценки домашней команды
        home_team_evals = TeamEvaluation.objects.filter(
            match=match,
            team=match.home_team
        ).aggregate(
            avg_tactics=Avg('tactics'),
            avg_effort=Avg('effort'),
            avg_organization=Avg('organization'),
            avg_mentality=Avg('mentality'),
            total=Count('id'),
        )
        
        # Оценки гостевой команды
        away_team_evals = TeamEvaluation.objects.filter(
            match=match,
            team=match.away_team
        ).aggregate(
            avg_tactics=Avg('tactics'),
            avg_effort=Avg('effort'),
            avg_organization=Avg('organization'),
            avg_mentality=Avg('mentality'),
            total=Count('id'),
        )
        
        # Оценки тренеров
        coach_aggregates = match.coach_aggregates.select_related('coach').all()[:2]
        
        # Статистика оценок
        total_match_evals = MatchEvaluation.objects.filter(match=match).count()
        total_player_evals = PlayerEvaluation.objects.filter(match=match).count()
        total_context_evals = ContextEvaluation.objects.filter(match=match).count()
        
        # Составы
        lineups = MatchLineup.objects.filter(
            match=match
        ).prefetch_related(
            'players__player',
            'players__player__team'
        ).order_by('side')
        
        # Мнение большинства (за кого болели)
        fan_support = ContextEvaluation.objects.filter(
            match=match
        ).exclude(
            supported_team__isnull=True
        ).values(
            'supported_team__id',
            'supported_team__name'
        ).annotate(
            count=Count('id')
        ).order_by('-count')[:2]
        
        # События матча
        events = match.events.select_related('player').order_by('minute')[:20]
        
        context.update({
            'voting_open': voting_open,
            'user_has_evaluated': user_has_evaluated,
            'match_aggregate': match_agg,
            'top_players': top_players,
            'worst_players': worst_players,
            'home_team_evals': home_team_evals,
            'away_team_evals': away_team_evals,
            'coach_aggregates': coach_aggregates,
            'total_match_evaluations': total_match_evals,
            'total_player_evaluations': total_player_evals,
            'total_context_evaluations': total_context_evals,
            'lineups': lineups,
            'fan_support': fan_support,
            'events': events,
            'page_title': f'{match.home_team.name} vs {match.away_team.name} — DOPX',
            'now': now,
        })
        return context

@require_http_methods(["GET"])
def match_events_partial(request, match_id):
    """HTMX partial для событий матча"""
    match = get_object_or_404(Match, id=match_id)
    events = match.events.select_related(
        'player', 'assist_player', 'player_out'
    ).order_by('minute', 'added_time', 'id')
    return render(request, 'matches/_match_events.html', {
        'match': match,
        'events': events,
    })