# referees/views.py
from django.db.models import Avg, Count, Q, Sum
from django.views.generic import ListView, DetailView
from core.utils import normalize_kz
from core.models import get_setting
from referees.models import Referee
from matches.models import Match
from aggregates.models import RefereeMatchAggregate
from aggregates.services import published_q, vote_weighted_avg
from seasons.models import Season


class RefereeListView(ListView):
    model = Referee
    template_name = 'referees/list.html'
    context_object_name = 'referees'
    paginate_by = 20

    def get_paginate_by(self, queryset):
        # Размер страницы — из настроек платформы.
        return get_setting("referees_list_page_size", self.paginate_by)

    def get_queryset(self):
        # Средние — по RefereeMatchAggregate. Матчи — за текущий сезон (?season=all — все).
        self.active_season = Season.get_primary_active()
        self.show_all = self.request.GET.get('season') == 'all'
        season_q = Q(match__season=self.active_season) if self.active_season and not self.show_all else Q()

        queryset = Referee.objects.filter(
            is_active=True
        ).annotate(
            # Имя аннотации совпадает с шаблоном
            total_matches=Count('match', filter=season_q, distinct=True),
            # Только матчи с закрытым голосованием.
            avg_influence=vote_weighted_avg(
                'match_aggregates__avg_influence', 'match_aggregates__total_votes',
                filter=published_q('match_aggregates__match__'),
            ),
            avg_decision_quality=vote_weighted_avg(
                'match_aggregates__avg_decision_quality', 'match_aggregates__total_votes',
                filter=published_q('match_aggregates__match__'),
            ),
        )

        # Поиск.
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
        context['active_season'] = self.active_season
        context['show_all'] = self.show_all
        return context


class RefereeDetailView(DetailView):
    model = Referee
    template_name = 'referees/detail.html'
    context_object_name = 'referee'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        referee = self.object

        # Страница судьи — тоже по активному сезону (?season=all — все), как в списке.
        active_season = Season.get_primary_active()
        show_all = self.request.GET.get('season') == 'all'
        season_kwargs = {'season': active_season} if active_season and not show_all else {}

        # Матчи судьи
        matches = Match.objects.filter(
            referee=referee, **season_kwargs
        ).select_related(
            'home_team', 'away_team', 'league', 'season'
        ).order_by('-start_time')[:20]

        # Оценки — агрегат по матчу (RefereeMatchAggregate), одна строка = один матч.
        eval_season_kwargs = {'match__season': active_season} if active_season and not show_all else {}
        evaluations = RefereeMatchAggregate.objects.filter(
            published_q(), referee=referee, **eval_season_kwargs
        ).select_related('match').order_by('-match__start_time')[:10]

        # Статистика: матчи и оценки отдельно
        agg_totals = RefereeMatchAggregate.objects.filter(
            published_q(), referee=referee, **eval_season_kwargs
        ).aggregate(
            total_evaluations=Sum('total_votes'),
            avg_influence=vote_weighted_avg('avg_influence'),
            avg_decision_quality=vote_weighted_avg('avg_decision_quality'),
            # Итоговый performance_score судьи с тултипом формулы.
            avg_performance_score=vote_weighted_avg('performance_score'),
        )
        stats = {
            # Матчи (факт)
            'total_matches': Match.objects.filter(referee=referee, **season_kwargs).count(),
            # Оценки (мнение) — из агрегата.
            'total_evaluations': agg_totals['total_evaluations'] or 0,
            'avg_influence': agg_totals['avg_influence'],
            'avg_decision_quality': agg_totals['avg_decision_quality'],
            'avg_performance_score': agg_totals['avg_performance_score'],
        }

        # Ближайший матч судьи, открытый для оценки (CTA).
        votable_match = next(
            (m for m in matches if m.status == 'finished' and m.is_voting_open()),
            None
        )

        context.update({
            'matches': matches,
            'evaluations': evaluations,
            'stats': stats,
            'votable_match': votable_match,
            'active_season': active_season,
            'show_all': show_all,
            'page_title': f'{referee.first_name} {referee.last_name} — DOPX',
        })
        return context