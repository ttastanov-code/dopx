# coaches/views.py
from django.views.generic import ListView, DetailView
from django.db.models import Count, Avg, Q
from coaches.models import Coach
from aggregates.models import CoachMatchAggregate
from matches.models import Match

class CoachListView(ListView):
    model = Coach
    template_name = 'coaches/list.html'
    context_object_name = 'coaches'
    paginate_by = 20
    
    def get_queryset(self):
        # ✅ FIX: Считаем фактические матчи из Match, а не агрегаты
        return Coach.objects.filter(
            is_active=True
        ).annotate(
            match_count=Count(
                'home_coached_matches', 
                filter=Q(home_coached_matches__status='finished'),
                distinct=True
            ) + Count(
                'away_coached_matches',
                filter=Q(away_coached_matches__status='finished'),
                distinct=True
            )
        ).order_by('last_name')
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = 'Все тренеры — DOPX'
        return context

class CoachDetailView(DetailView):
    model = Coach
    template_name = 'coaches/detail.html'
    context_object_name = 'coach'
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        coach = self.object
        
        # Матчи тренера — считаем из Match, а не из агрегатов!
        matches = Match.objects.filter(
            Q(home_coach=coach) | Q(away_coach=coach)
        ).select_related(
            'home_team', 'away_team', 'league', 'season'
        ).order_by('-start_time')[:20]
        
        # Агрегаты (для оценок)
        aggregates = CoachMatchAggregate.objects.filter(
            coach=coach
        ).select_related('match').order_by('-match__start_time')[:10]
        
        # ✅ Статистика: матчи считаем из Match, оценки — из агрегатов
        stats = {
            'total_matches': Match.objects.filter(  # ✅ ВСЕ матчи тренера
                Q(home_coach=coach) | Q(away_coach=coach)
            ).count(),
            'total_evaluations': CoachMatchAggregate.objects.filter(coach=coach).count(),
            'avg_tactics': CoachMatchAggregate.objects.filter(coach=coach).aggregate(
                avg=Avg('avg_tactics')
            )['avg'],
            'avg_substitutions': CoachMatchAggregate.objects.filter(coach=coach).aggregate(
                avg=Avg('avg_substitutions')
            )['avg'],
            'avg_management': CoachMatchAggregate.objects.filter(coach=coach).aggregate(
                avg=Avg('avg_management')
            )['avg'],
            'avg_impact': CoachMatchAggregate.objects.filter(coach=coach).aggregate(
                avg=Avg('avg_impact')
            )['avg'],
        }
        
        context.update({
            'matches': matches,
            'aggregates': aggregates,
            'stats': stats,
            'page_title': f'{coach.first_name} {coach.last_name} — DOPX',
        })
        return context