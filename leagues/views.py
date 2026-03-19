# leagues/views.py
from django.views.generic import ListView, DetailView
from django.db.models import Count, Avg
from leagues.models import League
from seasons.models import Season
from matches.models import Match

class LeagueListView(ListView):
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
    model = League
    template_name = 'leagues/detail.html'
    context_object_name = 'league'
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        league = self.object
        
        # Сезоны
        seasons = Season.objects.filter(
            league=league
        ).annotate(
            match_count=Count('match')
        ).order_by('-year')
        
        # Последние матчи
        recent_matches = Match.objects.filter(
            league=league
        ).select_related(
            'home_team', 'away_team', 'season'
        ).order_by('-start_time')[:10]
        
        context.update({
            'seasons': seasons,
            'recent_matches': recent_matches,
            'page_title': f'{league.name} — DOPX',
        })
        return context