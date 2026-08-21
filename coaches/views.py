# coaches/views.py
from django.views.generic import ListView, DetailView
from django.db.models import Count, Avg, Q, Prefetch
from core.utils import normalize_kz
from coaches.models import Coach
from teams.models import Team
from aggregates.models import CoachMatchAggregate
from matches.models import Match
from seasons.models import Season

class CoachListView(ListView):
    model = Coach
    template_name = 'coaches/list.html'
    context_object_name = 'coaches'
    paginate_by = 20

    def get_queryset(self):
        # Дефолт: только тренеры команд текущего сезона главной лиги — тот
        # же паттерн, что и TeamListView/PlayerListView, через TeamSeason
        # по team_id тренера. ?season=all снимает фильтр (нужно, например,
        # чтобы найти тренера вылетевшей команды). См. docs/BACKLOG.md,
        # находка 3.
        self.active_season = Season.get_primary_active()
        self.show_all = self.request.GET.get('season') == 'all'

        # Prefetch для coach.match_aggregates.first (карточка "Тактика" в
        # шаблоне) — без него N+1 на каждого тренера. Ordering модели
        # (-match__start_time) даёт тот же "последний матч", что и без prefetch.
        queryset = Coach.objects.filter(
            is_active=True
        ).prefetch_related(
            Prefetch(
                'match_aggregates',
                queryset=CoachMatchAggregate.objects.select_related('match').only(
                    'id', 'avg_tactics', 'coach_id', 'match_id', 'match__start_time'
                )
            )
        ).annotate(
            # РАНЬШЕ здесь был match_count через Match.home_coach/away_coach
            # — убран намеренно, не чиниться: KFF не хранит историю смен
            # тренера у команды, при смене тренера старые матчи в ИХ
            # системе задним числом переприкрепляются к новому — то есть
            # "21 матч" у тренера, отработавшего 2, был не багом нашего
            # импорта, а честным отражением того, что отдаёт источник
            # данных. Подтверждено вручную (сверка с сайтом KFF), см.
            # docs/BACKLOG.md, находка 4. evaluations_count — единственная
            # ПРАВДИВО тренеро-специфичная метрика: пользователь оценивает
            # конкретного тренера в момент, близкий к матчу, эта привязка
            # не переписывается задним числом.
            evaluations_count=Count('coach_evaluations', distinct=True)
        )

        if self.active_season and not self.show_all:
            queryset = queryset.filter(team__teamseason__season=self.active_season)

        # БАГ, КОТОРЫЙ ТУТ БЫЛ: поиск и фильтр по команде рисовались в
        # шаблоне (templates/coaches/list.html), но здесь никогда не
        # читались — форма молча ничего не делала. Coach.team — реальный
        # FK (parsers/kff/importers.py::get_or_create_coach его
        # заполняет, когда KFF присылает состав с тренерами), так что
        # фильтр физически имеет смысл, просто не был подключен.
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
        # Только команды, у которых реально есть привязанный тренер —
        # иначе в списке снова были бы варианты, ничего не находящие.
        context['teams'] = Team.objects.filter(coaches__isnull=False).distinct().order_by('name')
        return context

class CoachDetailView(DetailView):
    model = Coach
    template_name = 'coaches/detail.html'
    context_object_name = 'coach'
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        coach = self.object

        # РАНЬШЕ здесь был "matches" тренера через Match.home_coach/
        # away_coach — убрано намеренно, не баг для починки: KFF не хранит
        # историю смен тренера, при смене тренера в клубе старые матчи в
        # ИХ системе задним числом переприкрепляются к новому. Проверено
        # вручную (сверка с сайтом KFF) — на конкретном примере тренер,
        # отработавший 2 матча, показывал 21 (весь сезон клуба), потому
        # что "чей это матч" технически не сохраняется нигде, включая
        # источник данных. См. docs/BACKLOG.md, находка 4.
        #
        # Вместо личной истории тренера — форма ТЕКУЩЕЙ команды, честно
        # подписанная как командная, а не персональная статистика.
        team_matches = []
        if coach.team_id:
            team_matches = Match.objects.filter(
                Q(home_team=coach.team) | Q(away_team=coach.team),
                status='finished',
            ).select_related(
                'home_team', 'away_team', 'league', 'season'
            ).order_by('-start_time')[:10]

        # Агрегаты (для оценок) — ПРАВДИВО тренеро-специфичны: пользователь
        # оценивает конкретного тренера вскоре после матча, эта привязка не
        # переписывается задним числом при смене тренерского штаба.
        aggregates = CoachMatchAggregate.objects.filter(
            coach=coach
        ).select_related('match').order_by('-match__start_time')[:10]

        agg_totals = CoachMatchAggregate.objects.filter(coach=coach).aggregate(
            total_evaluations=Count('id'),
            avg_tactics=Avg('avg_tactics'),
            avg_substitutions=Avg('avg_substitutions'),
            avg_management=Avg('avg_management'),
            avg_impact=Avg('avg_impact'),
        )
        stats = {
            'total_evaluations': agg_totals['total_evaluations'] or 0,
            'avg_tactics': agg_totals['avg_tactics'],
            'avg_substitutions': agg_totals['avg_substitutions'],
            'avg_management': agg_totals['avg_management'],
            'avg_impact': agg_totals['avg_impact'],
        }
        # Условие видимости карточки "Средние оценки" — есть ли оценки
        # (total_evaluations): без оценок avg_* = None, и width: % в
        # прогресс-барах ломается на пустой строке.
        has_evaluations = stats['total_evaluations'] > 0

        context.update({
            'team_matches': team_matches,
            'aggregates': aggregates,
            'stats': stats,
            'has_evaluations': has_evaluations,
            'page_title': f'{coach.first_name} {coach.last_name} — DOPX',
        })
        return context