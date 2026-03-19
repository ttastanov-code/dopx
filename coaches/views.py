# coaches/views.py
from django.views.generic import ListView, DetailView
from django.db.models import Count, Avg
from coaches.models import Coach
from aggregates.models import CoachMatchAggregate
from matches.models import Match

class CoachListView(ListView):
    model = Coach
    template_name = 'coaches/list.html'
    context_object_name = 'coaches'
    paginate_by = 20
    
    def get_queryset(self):
        return Coach.objects.filter(is_active=True).order_by('last_name')
    
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
        
        # Матчи тренера
        matches = Match.objects.filter(
            home_coach=coach
        ).select_related(
            'home_team', 'away_team', 'league', 'season'
        ).order_by('-start_time')[:20]
        
        # Агрегаты
        aggregates = CoachMatchAggregate.objects.filter(
            coach=coach
        ).select_related('match').order_by('-match__start_time')[:10]
        
        # Статистика
        stats = CoachMatchAggregate.objects.filter(coach=coach).aggregate(
            avg_tactics=Avg('avg_tactics'),
            avg_substitutions=Avg('avg_substitutions'),
            avg_management=Avg('avg_management'),
            avg_impact=Avg('avg_impact'),
            total_matches=Count('id'),
        )
        
        context.update({
            'matches': matches,
            'aggregates': aggregates,
            'stats': stats,
            'page_title': f'{coach.first_name} {coach.last_name} — DOPX',
        })
        return context