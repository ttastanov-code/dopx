# referees/views.py
from django.views.generic import ListView, DetailView
from django.db.models import Avg, Count, Sum
from core.utils import normalize_kz
from referees.models import Referee
from matches.models import Match
from aggregates.models import RefereeMatchAggregate


class RefereeListView(ListView):
    model = Referee
    template_name = 'referees/list.html'
    context_object_name = 'referees'
    paginate_by = 20

    def get_queryset(self):
        # 2026-08-23: раньше здесь были Subquery по RefereeEvaluation
        # НАПРЯМУЮ (сырые голоса, без веса пользователя, без винзоризации).
        # Теперь просто Avg() по уже готовому, взвешенному
        # RefereeMatchAggregate (related_name='match_aggregates', см.
        # aggregates/tasks.py::recalculate_referee_aggregates) — не только
        # честнее, но и проще: обычный Avg() через join вместо двух
        # Subquery/OuterRef.
        queryset = Referee.objects.filter(
            is_active=True
        ).annotate(
            # ✅ Имя аннотации должно совпадать с шаблоном!
            total_matches=Count('match', distinct=True),
            avg_influence=Avg('match_aggregates__avg_influence'),
            avg_decision_quality=Avg('match_aggregates__avg_decision_quality'),
        )

        # БАГ, КОТОРЫЙ ТУТ БЫЛ: строка поиска в шаблоне рисовалась, но
        # queryset её никогда не читал — поиск был чисто декоративным.
        search = self.request.GET.get('q')
        if search:
            normalized_query = normalize_kz(search)
            matching_ids = [
                r.id for r in Referee.objects.only('id', 'first_name', 'last_name')
                if normalized_query in normalize_kz(f"{r.first_name} {r.last_name}")
            ]
            queryset = queryset.filter(id__in=matching_ids)

        return queryset.order_by('last_name')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = 'Все судьи — DOPX'
        context['search_query'] = self.request.GET.get('q', '')
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

        # ✅ Оценки — 2026-08-23: раньше это были СЫРЫЕ индивидуальные
        # RefereeEvaluation (до 10 последних ГОЛОСОВ, не матчей — при
        # нескольких оценивших один и тот же матч мог занять несколько
        # строк таблицы, и любой отдельный непроверенный голос попадал в
        # витрину как есть, без веса/винзоризации). Теперь — уже готовый,
        # взвешенный агрегат ПО МАТЧУ (RefereeMatchAggregate, см.
        # aggregates/tasks.py::recalculate_referee_aggregates): одна
        # строка = один матч.
        evaluations = RefereeMatchAggregate.objects.filter(
            referee=referee
        ).select_related('match').order_by('-match__start_time')[:10]

        # ✅ Статистика: разделяем матчи и оценки
        agg_totals = RefereeMatchAggregate.objects.filter(referee=referee).aggregate(
            total_evaluations=Sum('total_votes'),
            avg_influence=Avg('avg_influence'),
            avg_decision_quality=Avg('avg_decision_quality'),
        )
        stats = {
            # Матчи (факт)
            'total_matches': Match.objects.filter(referee=referee).count(),
            # Оценки (мнение) — берём из готового агрегата, не RefereeEvaluation.
            'total_evaluations': agg_totals['total_evaluations'] or 0,
            'avg_influence': agg_totals['avg_influence'],
            'avg_decision_quality': agg_totals['avg_decision_quality'],
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