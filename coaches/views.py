# coaches/views.py
from django.views.generic import ListView, DetailView
from django.db.models import Count, Avg, Q, Prefetch, Sum
from core.utils import normalize_kz
from core.models import get_setting
from coaches.models import Coach
from teams.models import Team
from aggregates.models import CoachMatchAggregate
from aggregates.services import min_votes_for_display, published_q
from aggregates.services import vote_weighted_avg
from matches.models import Match
from seasons.models import Season

class CoachListView(ListView):
    model = Coach
    template_name = 'coaches/list.html'
    context_object_name = 'coaches'
    paginate_by = 20

    def get_paginate_by(self, queryset):
        # Размер страницы — из настроек платформы.
        return get_setting("coaches_list_page_size", self.paginate_by)

    def get_queryset(self):
        # По умолчанию — тренеры команд текущего сезона. ?season=all — все.
        self.active_season = Season.get_primary_active()
        self.show_all = self.request.GET.get('season') == 'all'

        # Prefetch последнего агрегата — без N+1.
        queryset = Coach.objects.filter(
            is_active=True
        ).prefetch_related(
            Prefetch(
                'match_aggregates',
                queryset=CoachMatchAggregate.objects.filter(published_q()).select_related('match').only(
                    'id', 'avg_tactics', 'coach_id', 'match_id', 'match__start_time'
                )
            )
        ).annotate(
            # Число матчей тренера не показываем — источник не хранит историю смен тренеров.
            # evaluations_count — достоверная метрика.
            evaluations_count=Count('coach_evaluations', distinct=True)
        )

        if self.active_season and not self.show_all:
            queryset = queryset.filter(team__teamseason__season=self.active_season)

        # Поиск и фильтр по команде.
        search = self.request.GET.get('q')
        if search:
            normalized_query = normalize_kz(search)
            matching_ids = [
                c.id for c in Coach.objects.only('id', 'first_name', 'last_name')
                if normalized_query in normalize_kz(f"{c.first_name} {c.last_name}")
            ]
            queryset = queryset.filter(id__in=matching_ids)
        team_id = self.request.GET.get('team')
        if team_id:
            queryset = queryset.filter(team_id=team_id)

        return queryset.order_by('last_name')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = 'Все тренеры — DOPX'
        context['search_query'] = self.request.GET.get('q', '')
        context['active_season'] = self.active_season
        context['show_all'] = self.show_all
        # Команды с тренером в текущем сезоне (кроме ?season=all).
        teams_qs = Team.objects.filter(coaches__isnull=False)
        if self.active_season and not self.show_all:
            teams_qs = teams_qs.filter(teamseason__season=self.active_season)
        context['teams'] = teams_qs.distinct().order_by('name')
        return context

class CoachDetailView(DetailView):
    model = Coach
    template_name = 'coaches/detail.html'
    context_object_name = 'coach'
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        coach = self.object

        # Вместо матчей тренера — форма текущей команды (подписана как командная).
        team_matches = []
        if coach.team_id:
            team_matches = Match.objects.filter(
                Q(home_team=coach.team) | Q(away_team=coach.team),
                status='finished',
            ).select_related(
                'home_team', 'away_team', 'league', 'season'
            ).order_by('-start_time')[:10]

        # Агрегаты оценок тренера.
        # Только матчи с закрытым голосованием.
        aggregates = CoachMatchAggregate.objects.filter(
            published_q(), coach=coach
        ).select_related('match').order_by('-match__start_time')[:10]

        agg_totals = CoachMatchAggregate.objects.filter(published_q(), coach=coach).aggregate(
            total_evaluations=Count('id'),
            # Алиас не total_votes — иначе конфликт с Sum('total_votes') в vote_weighted_avg.
            votes_sum=Sum('total_votes'),
            avg_tactics=vote_weighted_avg('avg_tactics'),
            avg_substitutions=vote_weighted_avg('avg_substitutions'),
            avg_management=vote_weighted_avg('avg_management'),
            avg_impact=vote_weighted_avg('avg_impact'),
        )
        stats = {
            'total_evaluations': agg_totals['total_evaluations'] or 0,
            'total_votes': agg_totals['votes_sum'] or 0,
            'avg_tactics': agg_totals['avg_tactics'],
            'avg_substitutions': agg_totals['avg_substitutions'],
            'avg_management': agg_totals['avg_management'],
            'avg_impact': agg_totals['avg_impact'],
        }
        # Карточка «Средние оценки» — только если есть оценки.
        has_evaluations = stats['total_evaluations'] > 0

        # has_enough_votes — можно ли доверять цифре (MIN_VOTES_FOR_DISPLAY).
        has_enough_votes = stats['total_votes'] >= min_votes_for_display()

        context.update({
            'team_matches': team_matches,
            'aggregates': aggregates,
            'stats': stats,
            'has_evaluations': has_evaluations,
            'has_enough_votes': has_enough_votes,
            'page_title': f'{coach.first_name} {coach.last_name} — DOPX',
        })
        return context