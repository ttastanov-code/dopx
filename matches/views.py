# matches/views.py
import json

from django.shortcuts import render, get_object_or_404
from django.urls import reverse
from django.views.generic import ListView, DetailView
from django.utils import timezone
from django.db.models import Count, Q, Case, When, Value, IntegerField
from matches.models import Match
from aggregates.models import MatchAggregate, PlayerMatchAggregate, TeamMatchAggregate
from evaluations.models import PlayerEvaluation, MatchEvaluation, ContextEvaluation, EvaluationSession
from lineups.models import MatchLineup
from seasons.models import Season
from leagues.models import League
from predictions.services import bulk_prediction_data
import logging
from django.views.decorators.http import require_http_methods

logger = logging.getLogger(__name__)

class MatchListView(ListView):
    """Список всех матчей с фильтрами"""
    model = Match
    template_name = 'matches/list.html'
    context_object_name = 'matches'
    paginate_by = 20
    
    def get_queryset(self):
        queryset = Match.objects.select_related(
            'home_team',
            'away_team',
            'league',
            'season',
            'stadium'
        ).prefetch_related(
            'aggregate',
            'home_team__rivals',  # для match.is_derby — иначе N+1 на каждой карточке
        )
        
        # Фильтр по статусу
        status = self.request.GET.get('status')
        
        if status == 'scheduled':
            # Запланированные - от начала года до конца
            queryset = queryset.filter(status='scheduled').order_by('start_time')
        elif status == 'live':
            # Live - сначала матчи, которые раньше начались
            queryset = queryset.filter(status='live').order_by('start_time')
        elif status == 'finished':
            # Завершенные - ближе к сегодняшнему дню сначала
            queryset = queryset.filter(status='finished').order_by('-start_time')
        elif status == 'postponed':
            # 'postponed' ИЛИ (was_rescheduled=True и ещё не сыгран/не отменён)
            # — см. docs/adr/0013-match-list-filter-and-sort-fixes.md, находка №1.
            queryset = queryset.filter(
                Q(status='postponed') |
                Q(was_rescheduled=True, status__in=['scheduled', 'live'])
            ).order_by('-start_time')
        elif status == 'cancelled':
            queryset = queryset.filter(status='cancelled').order_by('-start_time')
        elif status == 'votable':
            # Матчи, доступные для оценки прямо сейчас — то же условие, что
            # и Match.is_voting_open() и stats.active_voting (core/views.py),
            # чтобы число на карточке и список за ней совпадали 1:1.
            # Сортировка по остатку времени — те, что вот-вот закроются, первыми.
            queryset = queryset.filter(
                status='finished', voting_open_until__gte=timezone.now()
            ).order_by('voting_open_until')
        elif status == 'evaluated':
            # Виртуальный статус (не поле Match.status), только по прямой
            # ссылке из profile/dashboard.html. Источник истины —
            # EvaluationSession.status='completed'. См.
            # docs/adr/0013-match-list-filter-and-sort-fixes.md, находка №2.
            if self.request.user.is_authenticated:
                queryset = queryset.filter(
                    evaluation_sessions__user=self.request.user,
                    evaluation_sessions__status='completed',
                ).order_by('-evaluation_sessions__completed_at')
            else:
                queryset = queryset.none()
        else:
            # Монотонный порядок по start_time (не по близости к "сейчас" —
            # ломало {% regroup %} по дате). "Актуальное первым" достигается
            # стартовой страницей в paginate_queryset(), не сортировкой. См.
            # docs/adr/0013-match-list-filter-and-sort-fixes.md, находка №3.
            queryset = queryset.order_by('start_time')
        
        # Фильтр по лиге
        league_id = self.request.GET.get('league')
        if league_id:
            queryset = queryset.filter(league_id=league_id)
        
        # Фильтр по сезону
        season_id = self.request.GET.get('season')
        if season_id:
            queryset = queryset.filter(season_id=season_id)

        # Фильтр по туру — прямой ответ на "непонятно какой тур из-за
        # переносов": группировка списка по дате (ниже, {% regroup %})
        # разваливается для перенесённого матча — start_time может
        # оказаться где угодно. Номер тура — устойчивый ориентир, не
        # меняется вместе с датой (см. Match.tour, docs/BACKLOG.md).
        tour = self.request.GET.get('tour')
        if tour:
            queryset = queryset.filter(tour=tour)

        return queryset

    def paginate_queryset(self, queryset, page_size):
        """
        Дефолтный список (без ?status=) отсортирован хронологически по
        возрастанию (см. get_queryset()) — без вмешательства первая
        страница показывала бы самые старые матчи всего сезона, а не то,
        что реально интересно пользователю прямо сейчас. Подбираем номер
        страницы так, чтобы открыться на той, что содержит первый ещё не
        начавшийся матч (т.е. "сегодня/дальше").

        НАЙДЕНО (2026-09-01, жалоба пользователя: "открывает на одну
        страницу раньше сегодняшней даты"): раньше здесь ЕЩЁ и отступали на
        3 позиции назад (`count() - 3`) "чтобы несколько последних
        результатов тоже было видно сразу". Но при фиксированных страницах
        паджинатора отступ на 3 позиции НАЗАД иногда означает отступ на
        ЦЕЛУЮ СТРАНИЦУ назад — ровно когда индекс первого будущего матча
        оказывается близко к границе страницы (остаток от деления на
        page_size — 0, 1 или 2). Например при page_size=20 и 40 уже прошедших
        матчах: индекс первого будущего — 40, "минус 3" даёт 37, а 37 и 40
        лежат на РАЗНЫХ страницах паджинатора (2-я и 3-я) — открывалась 2-я,
        где вообще нет ни одного будущего матча, только прошедшие. Смысла в
        отступе назад при фиксированных страницах нет вообще: если индекс
        первого будущего матча не у самой границы страницы, несколько
        прошедших и так попадают на ту же страницу естественным образом;
        если он у границы — искусственный отступ ломает страницу целиком,
        а не помогает. Проще и правильнее: открывать страницу, которая
        СОДЕРЖИТ первый будущий матч, без искусственного отступа.

        Работает только пока пользователь НЕ указал ни свою страницу, ни
        фильтр по статусу явно — у статусных веток (scheduled/finished/…)
        сортировка уже осмысленная сама по себе (ближайшие/самые свежие
        сверху), там "прыжок" на нужную страницу не нужен.
        """
        page_requested = self.kwargs.get(self.page_kwarg) or self.request.GET.get(self.page_kwarg)
        if not page_requested and not self.request.GET.get('status'):
            first_upcoming_index = queryset.filter(start_time__lt=timezone.now()).count()
            self.kwargs[self.page_kwarg] = first_upcoming_index // page_size + 1
        return super().paginate_queryset(queryset, page_size)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        current_status = self.request.GET.get('status', '')
        context['page_title'] = {
            'votable': 'Матчи для оценки — DOPX',
            'evaluated': 'Оценённые мной матчи — DOPX',
        }.get(current_status, 'Все матчи — DOPX')
        context['current_status'] = current_status
        context['current_league'] = self.request.GET.get('league', '')
        context['current_season'] = self.request.GET.get('season', '')
        context['current_tour'] = self.request.GET.get('tour', '')
        context['leagues'] = League.objects.all()[:10]
        context['seasons'] = Season.objects.filter(is_active=True)[:5]
        # Только туры активного сезона — иначе список рос бы вечно номерами
        # из прошлых сезонов вперемешку. У матчей без tour (ещё не
        # пересинканы после добавления поля) exclude(tour__isnull=True).
        active_season = Season.objects.filter(is_active=True).first()
        tours_qs = Match.objects.exclude(tour__isnull=True)
        if active_season:
            tours_qs = tours_qs.filter(season=active_season)
        context['tours'] = tours_qs.values_list('tour', flat=True).distinct().order_by('tour')
        context['now'] = timezone.now()

        # Инлайн-виджет прогноза 1X2 прямо на карточке (без перехода на
        # страницу матча) — запрос пользователя 2026-08-29. Считаем bulk'ом
        # на уже отпагинированную страницу (context[context_object_name] —
        # только 20 матчей максимум), не на весь queryset, см. докстринг
        # bulk_prediction_data(). Атрибуты вешаются прямо на объекты Match
        # текущей страницы — так шаблон обращается к ним как к обычным
        # полям (match.list_prediction_counts), без кастомного dict-lookup
        # фильтра в Django templates.
        page_matches = context.get(self.context_object_name) or []
        prediction_data = bulk_prediction_data(page_matches, self.request.user)
        for match in page_matches:
            data = prediction_data.get(match.id)
            if data:
                match.list_prediction_counts = data['counts']
                match.list_my_prediction = data['my_prediction']

        # "Оценить" на карточке матча вело в тупик для тех, кто уже оценил
        # этот матч — EvaluateContextView.dispatch() всё равно редиректит
        # такого пользователя назад с "Вы уже оценили этот матч". Отражаем
        # это в списке сразу, одним bulk-запросом на страницу (20 матчей),
        # а не N запросами по одному на карточку.
        if self.request.user.is_authenticated:
            evaluated_match_ids = set(
                EvaluationSession.objects.filter(
                    user=self.request.user,
                    match_id__in=[m.id for m in page_matches],
                    status='completed',
                ).values_list('match_id', flat=True)
            )
            for match in page_matches:
                match.user_has_evaluated = match.id in evaluated_match_ids
        else:
            for match in page_matches:
                match.user_has_evaluated = False

        return context

class MatchDetailView(DetailView):
    """Детальная страница матча + результаты оценок"""
    model = Match
    template_name = 'matches/detail.html'
    context_object_name = 'match'
    
    def get_queryset(self):
        return Match.objects.select_related(
            'home_team',
            'away_team',
            'league',
            'season',
            'home_coach',
            'away_coach',
            'referee',
            'stadium'
        ).prefetch_related(
            'lineups__players__player',
            'lineups__players__player__team',
            'aggregate',
            'player_aggregates__player',
            'player_aggregates__player__team',
            'events',
            'coach_aggregates__coach',
            'home_team__rivals',  # для match.is_derby
        )
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        match = self.object
        now = timezone.now()

        # voting_open / user_has_evaluated / user_has_pulse_reactions / share_text —
        # общая функция match_action_context, используется и здесь, и в
        # match_header_partial (live-поллинг шапки), чтобы обе точки входа
        # считали CTA одинаково.
        action_context = match_action_context(self.request, match)

        # Агрегаты матча
        match_agg = getattr(match, 'aggregate', None)
        
        # Топ 5 игроков матча. total_votes__gte=MIN_VOTES_FOR_DISPLAY — иначе
        # один голос "10/10 от друга" обходит игрока с 40 честными оценками.
        from aggregates.services import MIN_VOTES_FOR_DISPLAY

        top_players = PlayerMatchAggregate.objects.filter(
            match=match, total_votes__gte=MIN_VOTES_FOR_DISPLAY
        ).select_related(
            'player',
            'player__team'
        ).order_by('-performance_score')[:5]

        # Худшие 3 игрока — та же логика: не топить в антирейтинге по 1-2 предвзятым оценкам.
        worst_players = PlayerMatchAggregate.objects.filter(
            match=match, total_votes__gte=MIN_VOTES_FOR_DISPLAY
        ).select_related(
            'player',
            'player__team'
        ).order_by('performance_score')[:3]
        
        # Оценки команд за ЭТОТ матч — раньше считались Avg() напрямую по
        # TeamEvaluation (без веса пользователя, без винзоризации, без
        # защиты от сговора фан-базы). 2026-08-23: читаем уже готовый,
        # взвешенный и винзоризованный TeamMatchAggregate (пересчитывается
        # асинхронно, см. aggregates/tasks.py::recalculate_team_aggregates) —
        # словарь с теми же ключами (avg_tactics/avg_effort/avg_organization/
        # avg_mentality/total), чтобы не трогать шаблон.
        team_aggs_by_team_id = {
            agg.team_id: agg
            for agg in TeamMatchAggregate.objects.filter(match=match)
        }

        def _team_evals_dict(team_id):
            agg = team_aggs_by_team_id.get(team_id)
            if not agg:
                return {'avg_tactics': None, 'avg_effort': None, 'avg_organization': None, 'avg_mentality': None, 'total': 0}
            return {
                'avg_tactics': agg.avg_tactics,
                'avg_effort': agg.avg_effort,
                'avg_organization': agg.avg_organization,
                'avg_mentality': agg.avg_mentality,
                'total': agg.total_votes,
            }

        home_team_evals = _team_evals_dict(match.home_team_id)
        away_team_evals = _team_evals_dict(match.away_team_id)
        
        # Оценки тренеров
        coach_aggregates = match.coach_aggregates.select_related('coach').all()[:2]
        
        # Статистика оценок
        total_match_evals = MatchEvaluation.objects.filter(match=match).count()
        total_player_evals = PlayerEvaluation.objects.filter(match=match).count()
        total_context_evals = ContextEvaluation.objects.filter(match=match).count()
        
        # Составы. side — CharField с choices "home"/"away" (lineups/models.py),
        # обычный .order_by('side') сортирует по алфавиту строк, а "away" <
        # "home" — гостевой состав всегда оказывался выше домашнего. Явно
        # мапим порядок через Case/When: home=0, away=1.
        lineups = MatchLineup.objects.filter(
            match=match
        ).prefetch_related(
            'players__player',
            'players__player__team'
        ).annotate(
            side_order=Case(
                When(side='home', then=Value(0)),
                When(side='away', then=Value(1)),
                default=Value(2),
                output_field=IntegerField(),
            )
        ).order_by('side_order')
        
        # Мнение большинства (за кого болели)
        fan_support = ContextEvaluation.objects.filter(
            match=match
        ).exclude(
            supported_team__isnull=True
        ).values(
            'supported_team__id',
            'supported_team__name'
        ).annotate(
            count=Count('id')
        ).order_by('-count')[:2]
        
        # События матча
        events = match.events.select_related('player').order_by('minute')[:20]
        
        context.update(action_context)
        context.update({
            'match_aggregate': match_agg,
            'top_players': top_players,
            'worst_players': worst_players,
            'home_team_evals': home_team_evals,
            'away_team_evals': away_team_evals,
            'coach_aggregates': coach_aggregates,
            'total_match_evaluations': total_match_evals,
            'total_player_evaluations': total_player_evals,
            'total_context_evaluations': total_context_evals,
            'lineups': lineups,
            'fan_support': fan_support,
            'events': events,
            'page_title': f'{match.home_team.name} vs {match.away_team.name} — DOPX',
            'now': now,
        })

        # SEO: meta_description + schema.org (SportsEvent) — используются в
        # <head> базового шаблона (см. templates/base.html) и в
        # templates/matches/detail.html через {% block schema %}. Через
        # json.dumps, а не ручную интерполяцию Django-переменных внутри
        # <script> — сырая подстановка имени команды/игрока со спецсимволами
        # (кавычки, </script>) могла бы сломать JSON или открыть XSS.
        context['meta_description'] = (
            f"Оценка матча {match.home_team.name} {match.get_score_display()} "
            f"{match.away_team.name} от болельщиков DOPX. Рейтинги игроков, тренеров и судьи."
        )
        # Шер-карточка (core/services/share_cards.py) — абсолютный URL,
        # соцсети-скрейперы (Telegram/WhatsApp) не резолвят относительные
        # og:image. Тот же путь используется и на кнопках "Поделиться" ниже.
        context['og_image'] = self.request.build_absolute_uri(
            reverse('core:match_share_card', args=[match.id])
        )
        schema = {
            "@context": "https://schema.org",
            "@type": "SportsEvent",
            "name": f"{match.home_team.name} vs {match.away_team.name}",
            "startDate": match.start_time.isoformat(),
            "location": {"@type": "Place", "name": match.stadium.name if match.stadium else (match.home_team.city or "Казахстан")},
            "competitor": [
                {"@type": "SportsTeam", "name": match.home_team.name},
                {"@type": "SportsTeam", "name": match.away_team.name},
            ],
            "description": context['meta_description'],
        }
        # .replace('</', '<\/') — json.dumps НЕ экранирует '</', поэтому
        # название команды вида "</script><script>..." могло бы оборвать тег
        # application/ld+json раньше конца JSON (шаблон рендерит эту строку
        # через |safe, см. templates/matches/detail.html).
        context['schema_json'] = json.dumps(schema, ensure_ascii=False).replace('</', '<\\/')
        return context

@require_http_methods(["GET"])
def match_events_partial(request, match_id):
    """HTMX partial для событий матча"""
    match = get_object_or_404(Match, id=match_id)
    events = match.events.select_related(
        'player', 'assist_player', 'player_out'
    ).order_by('minute', 'added_time', 'id')
    return render(request, 'matches/_match_events.html', {
        'match': match,
        'events': events,
    })


def match_action_context(request, match):
    """CTA-флаги матча (голосование/оценка/пульс). Общий код для MatchDetailView и match_header_partial."""
    voting_open = match.voting_open_until > timezone.now() and match.status == 'finished'
    user_has_evaluated = False
    user_has_pulse_reactions = False
    if request.user.is_authenticated:
        user_has_evaluated = EvaluationSession.objects.filter(
            user=request.user, match=match, status='completed'
        ).exists()
        if not user_has_evaluated and match.status == 'finished':
            from events.models import EventReaction
            user_has_pulse_reactions = EventReaction.objects.filter(
                user=request.user, match_event__match=match
            ).exists()
    return {
        'voting_open': voting_open,
        'user_has_evaluated': user_has_evaluated,
        'user_has_pulse_reactions': user_has_pulse_reactions,
        'share_text': (
            f"{match.home_team.name} {match.get_score_display()} {match.away_team.name} — "
            f"смотрите оценки болельщиков на DOPX"
        ),
    }


@require_http_methods(["GET"])
def match_header_partial(request, match_id):
    """Live-partial шапки матча: счёт, статус, CTA. Опрашивается каждые 20с, пока матч live."""
    match = get_object_or_404(
        Match.objects.select_related(
            'home_team', 'away_team', 'league', 'season',
            'home_coach', 'away_coach', 'referee', 'stadium',
        ).prefetch_related('home_team__rivals'),  # rivals нужен для match.is_derby
        id=match_id,
    )
    context = match_action_context(request, match)
    context['match'] = match
    return render(request, 'matches/_match_header.html', context)