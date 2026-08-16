# referees/views.py
from django.views.generic import ListView, DetailView
from django.db.models import Count, Avg, OuterRef, Subquery
from referees.models import Referee
from matches.models import Match
from evaluations.models import RefereeEvaluation


class RefereeListView(ListView):
    model = Referee
    template_name = 'referees/list.html'
    context_object_name = 'referees'
    paginate_by = 20
    
    def get_queryset(self):
        # Подзапросы для средних оценок (чтобы не было дубликатов)
        avg_influence_subquery = RefereeEvaluation.objects.filter(
            match__referee=OuterRef('pk')
        ).values('match__referee').annotate(
            avg_inf=Avg('influence_score')
        ).values('avg_inf')
        
        avg_quality_subquery = RefereeEvaluation.objects.filter(
            match__referee=OuterRef('pk')
        ).values('match__referee').annotate(
            avg_qual=Avg('decision_quality')
        ).values('avg_qual')
        
        return Referee.objects.filter(
            is_active=True
        ).annotate(
            # ✅ Имя аннотации должно совпадать с шаблоном!
            total_matches=Count('match', distinct=True),
            avg_influence=Subquery(avg_influence_subquery),
            avg_decision_quality=Subquery(avg_quality_subquery),
        ).order_by('last_name')
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = 'Все судьи — DOPX'
        # countries можно убрать, если фильтр закомментирован
        return context


class RefereeDetailView(DetailView):
    model = Referee
    template_name = 'referees/detail.html'
    context_object_name = 'referee'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        referee = self.object

        # ✅ Матчи судьи (по факту, а не по оценкам)
        matches = Match.objects.filter(
            referee=referee
        ).select_related(
            'home_team', 'away_team', 'league', 'season'
        ).order_by('-start_time')[:20]

        # ✅ Оценки (отдельно)
        evaluations = RefereeEvaluation.objects.filter(
            match__referee=referee
        ).select_related('match').order_by('-match__start_time')[:10]

        # ✅ Статистика: разделяем матчи и оценки
        stats = {
            # Матчи (факт)
            'total_matches': Match.objects.filter(referee=referee).count(),
            # Оценки (мнение)
            'total_evaluations': RefereeEvaluation.objects.filter(
                match__referee=referee
            ).count(),
            'avg_influence': RefereeEvaluation.objects.filter(
                match__referee=referee
            ).aggregate(avg=Avg('influence_score'))['avg'],
            'avg_decision_quality': RefereeEvaluation.objects.filter(
                match__referee=referee
            ).aggregate(avg=Avg('decision_quality'))['avg'],
        }

        # НОВОЕ: ближайший обслуженный матч, который ещё можно оценить —
        # для CTA в пустых состояниях (тот же паттерн, что на страницах
        # команды и игрока).
        votable_match = next(
            (m for m in matches if m.status == 'finished' and m.is_voting_open()),
            None
        )

        context.update({
            'matches': matches,
            'evaluations': evaluations,
            'stats': stats,
            'votable_match': votable_match,
            'page_title': f'{referee.first_name} {referee.last_name} — DOPX',
        })
        return context