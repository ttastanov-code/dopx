# coaches/views.py
from django.views.generic import ListView, DetailView
from django.db.models import Count, Avg, Q, Prefetch
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
        # ✅ ИСПРАВЛЕНО: шаблон обращается к coach.match_aggregates.first для
        # карточки "Тактика" — без prefetch это был N+1 (по доп. запросу на
        # каждого из 20 тренеров на странице). CoachMatchAggregate.Meta.ordering
        # = ['-match__start_time'], поэтому prefetch без явного order_by даёт
        # тот же самый "последний матч", что и .first() без него.
        return Coach.objects.filter(
            is_active=True
        ).prefetch_related(
            Prefetch(
                'match_aggregates',
                queryset=CoachMatchAggregate.objects.select_related('match').only(
                    'id', 'avg_tactics', 'coach_id', 'match_id', 'match__start_time'
                )
            )
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
        coach_aggs = CoachMatchAggregate.objects.filter(coach=coach)
        agg_totals = coach_aggs.aggregate(
            total_evaluations=Count('id'),
            avg_tactics=Avg('avg_tactics'),
            avg_substitutions=Avg('avg_substitutions'),
            avg_management=Avg('avg_management'),
            avg_impact=Avg('avg_impact'),
        )
        stats = {
            'total_matches': Match.objects.filter(  # ✅ ВСЕ матчи тренера
                Q(home_coach=coach) | Q(away_coach=coach)
            ).count(),
            'total_evaluations': agg_totals['total_evaluations'] or 0,
            'avg_tactics': agg_totals['avg_tactics'],
            'avg_substitutions': agg_totals['avg_substitutions'],
            'avg_management': agg_totals['avg_management'],
            'avg_impact': agg_totals['avg_impact'],
        }
        # ✅ ИСПРАВЛЕНО: карточка "Средние оценки" раньше показывалась при
        # stats.total_matches (все матчи тренера), хотя данные в ней берутся
        # из CoachMatchAggregate (оценки болельщиков). Если тренер отработал
        # матчи, но их ещё никто не оценил, avg_* были None, а width: %
        # в прогресс-барах получал пустую строку — визуально сломанные бары.
        # Правильное условие — есть ли вообще оценки.
        has_evaluations = stats['total_evaluations'] > 0

        context.update({
            'matches': matches,
            'aggregates': aggregates,
            'stats': stats,
            'has_evaluations': has_evaluations,
            'page_title': f'{coach.first_name} {coach.last_name} — DOPX',
        })
        return context