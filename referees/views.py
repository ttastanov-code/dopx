# referees/views.py
from django.views.generic import ListView, DetailView
from django.db.models import Count, Avg
from referees.models import Referee
from matches.models import Match
from evaluations.models import RefereeEvaluation

class RefereeListView(ListView):
    model = Referee
    template_name = 'referees/list.html'
    context_object_name = 'referees'
    paginate_by = 20
    
    def get_queryset(self):
        return Referee.objects.filter(is_active=True).order_by('last_name')
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = 'Все судьи — DOPX'
        return context


class RefereeDetailView(DetailView):
    model = Referee
    template_name = 'referees/detail.html'
    context_object_name = 'referee'
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        referee = self.object
        
        # Матчи судьи
        matches = Match.objects.filter(
            referee=referee
        ).select_related(
            'home_team', 'away_team', 'league', 'season'
        ).order_by('-start_time')[:20]
        
        # Оценки
        evaluations = RefereeEvaluation.objects.filter(
            match__referee=referee
        ).select_related('match').order_by('-match__start_time')[:10]
        
        # Статистика
        stats = RefereeEvaluation.objects.filter(
            match__referee=referee
        ).aggregate(
            avg_influence=Avg('influence_score'),
            avg_decision_quality=Avg('decision_quality'),
            total_matches=Count('id'),
        )
        
        context.update({
            'matches': matches,
            'evaluations': evaluations,
            'stats': stats,
            'page_title': f'{referee.first_name} {referee.last_name} — DOPX',
        })
        return context