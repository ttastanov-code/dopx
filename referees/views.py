# referees/views.py
from django.db.models import Avg, Count, Q, Sum
from django.views.generic import ListView, DetailView
from core.utils import normalize_kz
from referees.models import Referee
from matches.models import Match
from aggregates.models import RefereeMatchAggregate
from seasons.models import Season


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
        #
        # 2026-09-09 (жалоба пользователя после полного бэкафилла 3
        # сезонов): "матчей" должно быть за текущий сезон, а не за всю
        # историю разом — тот же принцип, что и в TeamListView/PlayerListView
        # (teams/views.py, players/views.py). ?season=all снимает фильтр —
        # для единообразия с этими двумя страницами, хотя сам список судей
        # (в отличие от команд/игроков) и так не был season-scoped: судья
        # не привязан к сезону напрямую, только через свои матчи.
        self.active_season = Season.get_primary_active()
        self.show_all = self.request.GET.get('season') == 'all'
        season_q = Q(match__season=self.active_season) if self.active_season and not self.show_all else Q()

        queryset = Referee.objects.filter(
            is_active=True
        ).annotate(
            # ✅ Имя аннотации должно совпадать с шаблоном!
            total_matches=Count('match', filter=season_q, distinct=True),
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

        # 2026-09-11 (баг пользователя: у Нурзатбек Абдыкадырова список
        # судей показывал "0" матчей, а его же страница — "1"). Причина —
        # ТОТ ЖЕ класс несоответствия querysets, что уже чинили для
        # игроков/команд: RefereeListView.get_queryset() с 2026-09-09
        # считает total_matches ТОЛЬКО за активный сезон (Q(match__season=
        # active_season), см. комментарий там же), а эта страница до
        # сегодня считала матчи/оценки за ВСЮ историю разом — числа не
        # могли не разойтись. Приводим детальную страницу к тому же
        # умолчанию (активный сезон, ?season=all снимает фильтр) — тот же
        # переключатель _season_scope_toggle.html, что и на списке судей.
        active_season = Season.get_primary_active()
        show_all = self.request.GET.get('season') == 'all'
        season_kwargs = {'season': active_season} if active_season and not show_all else {}

        # ✅ Матчи судьи (по факту, а не по оценкам)
        matches = Match.objects.filter(
            referee=referee, **season_kwargs
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
        eval_season_kwargs = {'match__season': active_season} if active_season and not show_all else {}
        evaluations = RefereeMatchAggregate.objects.filter(
            referee=referee, **eval_season_kwargs
        ).select_related('match').order_by('-match__start_time')[:10]

        # ✅ Статистика: разделяем матчи и оценки
        agg_totals = RefereeMatchAggregate.objects.filter(
            referee=referee, **eval_season_kwargs
        ).aggregate(
            total_evaluations=Sum('total_votes'),
            avg_influence=Avg('avg_influence'),
            avg_decision_quality=Avg('avg_decision_quality'),
        )
        stats = {
            # Матчи (факт)
            'total_matches': Match.objects.filter(referee=referee, **season_kwargs).count(),
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
            'active_season': active_season,
            'show_all': show_all,
            'page_title': f'{referee.first_name} {referee.last_name} — DOPX',
        })
        return context