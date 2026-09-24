# matches/views.py
import json

from django.http import HttpResponse
from django.shortcuts import render, get_object_or_404
from django.urls import reverse
from django.views.generic import ListView, DetailView
from django.utils import timezone
from django.db.models import Count, Q, Case, When, Value, IntegerField
from matches.models import Match, MatchReaction
from matches.card_services import attach_card_extras
from matches.services import submit_match_reaction, reaction_counts, user_match_reaction
from aggregates.models import MatchAggregate, PlayerMatchAggregate, TeamMatchAggregate
from evaluations.models import PlayerEvaluation, MatchEvaluation, ContextEvaluation, EvaluationSession
from lineups.models import MatchLineup
from seasons.models import Season
from leagues.models import League
from core.utils import is_rate_limited
from core.models import get_setting
import logging
from django.views.decorators.http import require_http_methods, require_POST

logger = logging.getLogger(__name__)

class MatchListView(ListView):
    """Список матчей с фильтрами."""
    model = Match
    template_name = 'matches/list.html'
    context_object_name = 'matches'
    paginate_by = 20

    def get_paginate_by(self, queryset):
        # Размер страницы — из настроек платформы.
        return get_setting("matches_public_page_size", self.paginate_by)

    def get_queryset(self):
        queryset = Match.objects.select_related(
            'home_team',
            'away_team',
            'league',
            'season',
        ).prefetch_related(
            'aggregate',
            'home_team__rivals',  # для match.is_derby без N+1
        )
        
        # Фильтр по статусу
        status = self.request.GET.get('status')
        
        if status == 'scheduled':
            # Запланированные — ближайшие сначала
            queryset = queryset.filter(status='scheduled').order_by('start_time')
        elif status == 'live':
            # Live — раньше начавшиеся сначала
            queryset = queryset.filter(status='live').order_by('start_time')
        elif status == 'finished':
            # Завершённые — свежие сначала
            queryset = queryset.filter(status='finished').order_by('-start_time')
        elif status == 'postponed':
            # Перенесённые: 'postponed' или was_rescheduled и ещё не сыгран.
            # См. docs/adr/0013-match-list-filter-and-sort-fixes.md.
            queryset = queryset.filter(
                Q(status='postponed') |
                Q(was_rescheduled=True, status__in=['scheduled', 'live'])
            ).order_by('-start_time')
        elif status == 'cancelled':
            queryset = queryset.filter(status='cancelled').order_by('-start_time')
        elif status == 'votable':
            # Доступные для оценки — то же условие, что Match.is_voting_open(). Скоро закрывающиеся первыми.
            queryset = queryset.filter(
                status='finished', voting_open_until__gte=timezone.now()
            ).order_by('voting_open_until')
        elif status == 'evaluated':
            # Виртуальный статус «оценённые мной» (по EvaluationSession completed).
            if self.request.user.is_authenticated:
                queryset = queryset.filter(
                    evaluation_sessions__user=self.request.user,
                    evaluation_sessions__status='completed',
                ).order_by('-evaluation_sessions__completed_at')
            else:
                queryset = queryset.none()
        else:
            # Порядок по start_time (иначе ломается {% regroup %} по дате).
            # Стартовая страница — см. paginate_queryset().
            queryset = queryset.order_by('start_time')
        
        # Фильтр по лиге
        league_id = self.request.GET.get('league')
        if league_id:
            queryset = queryset.filter(league_id=league_id)
        
        # Фильтр по сезону
        season_id = self.request.GET.get('season')
        if season_id:
            queryset = queryset.filter(season_id=season_id)

        # Фильтр по туру — устойчив к переносам дат.
        tour = self.request.GET.get('tour')
        if tour:
            queryset = queryset.filter(tour=tour)

        return queryset

    def paginate_queryset(self, queryset, page_size):
        """Без ?status= и ?page= открываем страницу, где первый ещё не начавшийся матч."""
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
        # Все сезоны лиги (активный — только один).
        context['seasons'] = Season.objects.order_by('-year')[:10]
        # Туры выбранного в фильтре сезона (без фильтра — активного).
        season_param = self.request.GET.get('season', '').strip()
        if season_param:
            tours_season = Season.objects.filter(id=season_param).first()
        else:
            tours_season = Season.objects.filter(is_active=True).first()
        tours_qs = Match.objects.exclude(tour__isnull=True)
        if tours_season:
            tours_qs = tours_qs.filter(season=tours_season)
        context['tours'] = tours_qs.values_list('tour', flat=True).distinct().order_by('tour')
        context['now'] = timezone.now()

        # Данные карточек (прогноз, форма, H2H, мини-ДНК и т.д.) — одним bulk-вызовом
        # matches/card_services.py::attach_card_extras.
        page_matches = context.get(self.context_object_name) or []
        attach_card_extras(page_matches, self.request)

        return context

class MatchDetailView(DetailView):
    """Страница матча."""
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
        ).prefetch_related(
            'lineups__players__player',
            'lineups__players__player__team',
            'aggregate',
            'player_aggregates__player',
            'player_aggregates__player__team',
            'events',
            'coach_aggregates__coach',
            'home_team__rivals',  # для match.is_derby
            # Статистика команд за матч.
            'team_statistics__team',
        )
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        match = self.object
        now = timezone.now()

        # CTA-флаги — общая функция со шапкой для live-поллинга.
        action_context = match_action_context(self.request, match)

        match_agg = getattr(match, 'aggregate', None)
        
        # Топ-5 игроков матча (только с достаточным числом голосов).
        from aggregates.services import MIN_VOTES_FOR_DISPLAY

        top_players = PlayerMatchAggregate.objects.filter(
            match=match, total_votes__gte=MIN_VOTES_FOR_DISPLAY
        ).select_related(
            'player',
            'player__team'
        ).order_by('-performance_score')[:5]

        # Худшие 3 — с тем же порогом голосов.
        worst_players = PlayerMatchAggregate.objects.filter(
            match=match, total_votes__gte=MIN_VOTES_FOR_DISPLAY
        ).select_related(
            'player',
            'player__team'
        ).order_by('performance_score')[:3]

        # Оценка «по статистике» для топ/антитоп игроков (matches/stat_ratings.py).
        from matches.stat_ratings import stat_ratings_for_match

        stat_ratings = stat_ratings_for_match(match)
        top_players = list(top_players)
        worst_players = list(worst_players)
        for agg in top_players + worst_players:
            agg.stat_rating = stat_ratings.get(agg.player_id)

        # Оценки команд из TeamMatchAggregate (взвешенные и защищённые).
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
        
        coach_aggregates = match.coach_aggregates.select_related('coach').all()[:2]
        
        total_match_evals = MatchEvaluation.objects.filter(match=match).count()
        total_player_evals = PlayerEvaluation.objects.filter(match=match).count()
        total_context_evals = ContextEvaluation.objects.filter(match=match).count()
        
        # Составы: хозяева сначала.
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
        
        # За кого болели — список нужен и шаблону, и build_match_dna.
        fan_support = list(ContextEvaluation.objects.filter(
            match=match
        ).exclude(
            supported_team__isnull=True
        ).values(
            'supported_team__id',
            'supported_team__name'
        ).annotate(
            count=Count('id')
        ).order_by('-count')[:2])

        # Ширина полоски «За кого болели» — доля голосов, а не абсолютное число.
        fan_support_total = sum(row['count'] for row in fan_support)
        for row in fan_support:
            row['pct'] = round(row['count'] / fan_support_total * 100) if fan_support_total else 0

        # События матча (лимит — из настроек платформы).
        events = list(match.events.select_related('player').order_by('minute')[:get_setting("match_recent_events_limit", 20)])

        # «ДНК матча».
        from matches.services import build_match_dna

        referee_agg = match.referee_aggregates.first()
        # Для консенсуса нужны сырые голоса MatchEvaluation.
        match_evaluations = list(MatchEvaluation.objects.filter(match=match).only('entertainment', 'tension', 'fairness'))
        match_dna = build_match_dna(
            match, match_agg, events, referee_agg,
            match_evaluations=match_evaluations, top_players=list(top_players),
            worst_players=list(worst_players), fan_support=fan_support,
        )

        # Абсолютный URL карточки ДНК (для Web Share API).
        match_dna_share_url = (
            self.request.build_absolute_uri(reverse('core:match_dna_share_card', args=[match.id]))
            if match_dna else ''
        )

        # Статистика по team_id.
        team_stats_by_team_id = {
            stat.team_id: stat for stat in match.team_statistics.all()
        }
        home_team_stats = team_stats_by_team_id.get(match.home_team_id)
        away_team_stats = team_stats_by_team_id.get(match.away_team_id)

        # Строки для карточки статистики матча.
        def _stat_pair(field, label, suffix=''):
            return {
                'label': label,
                'suffix': suffix,
                'home': getattr(home_team_stats, field, None) if home_team_stats else None,
                'away': getattr(away_team_stats, field, None) if away_team_stats else None,
            }

        stat_rows = [
            _stat_pair('possession_percent', 'Владение мячом', '%'),
            # Опасные атаки — рядом с владением.
            _stat_pair('dangerous_attacks', 'Опасные атаки'),
            _stat_pair('shots', 'Удары'),
            _stat_pair('shots_on_goal', 'Удары в створ'),
            _stat_pair('corners', 'Угловые'),
            _stat_pair('fouls', 'Фолы'),
            _stat_pair('offsides', 'Офсайды'),
            _stat_pair('yellow_cards', 'Жёлтые карточки'),
            _stat_pair('red_cards', 'Красные карточки'),
            _stat_pair('xg', 'Ожидаемые голы (xG)'),
            _stat_pair('pass_accuracy', 'Точность передач', '%'),
            _stat_pair('key_passes', 'Ключевые передачи'),
        ]
        has_match_statistics = any(row['home'] is not None or row['away'] is not None for row in stat_rows)

        # Форма команд на момент матча.
        from teams.services import get_team_form, get_pre_match_standings_snapshot

        # Турнирная таблица на момент матча.
        standings_snapshot = get_pre_match_standings_snapshot(match)

        # Личные встречи этих команд.
        h2h_matches = list(
            Match.objects.filter(
                Q(home_team=match.home_team, away_team=match.away_team)
                | Q(home_team=match.away_team, away_team=match.home_team),
                status='finished',
            ).exclude(id=match.id)
            .select_related('home_team', 'away_team')
            .order_by('-start_time')[:5]
        )
        h2h_summary = None
        if h2h_matches:
            from matches.card_services import _summarize_h2h

            h2h_summary = _summarize_h2h(match, [
                {
                    'home_team_id': m.home_team_id, 'away_team_id': m.away_team_id,
                    'home_score': m.home_score, 'away_score': m.away_score,
                } for m in h2h_matches
            ])

        home_recent = Match.objects.filter(
            Q(home_team=match.home_team) | Q(away_team=match.home_team),
            status='finished', start_time__lt=match.start_time,
        ).select_related('home_team', 'away_team').order_by('-start_time')[:5]
        away_recent = Match.objects.filter(
            Q(home_team=match.away_team) | Q(away_team=match.away_team),
            status='finished', start_time__lt=match.start_time,
        ).select_related('home_team', 'away_team').order_by('-start_time')[:5]
        home_team_form = get_team_form(match.home_team, home_recent)
        away_team_form = get_team_form(match.away_team, away_recent)

        context.update(action_context)
        context.update({
            'match_aggregate': match_agg,
            'match_dna': match_dna,
            'match_dna_share_url': match_dna_share_url,
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
            'home_team_stats': home_team_stats,
            'away_team_stats': away_team_stats,
            'stat_rows': stat_rows,
            'has_match_statistics': has_match_statistics,
            'home_team_form': home_team_form,
            'away_team_form': away_team_form,
            'standings_snapshot': standings_snapshot,
            'h2h_matches': h2h_matches,
            'h2h_summary': h2h_summary,
            'page_title': f'{match.home_team.name} vs {match.away_team.name} — DOPX',
            'now': now,
        })

        # SEO: meta_description и schema.org SportsEvent (через json.dumps — без XSS).
        context['meta_description'] = (
            f"Оценка матча {match.home_team.name} {match.get_score_display()} "
            f"{match.away_team.name} от болельщиков DOPX. Рейтинги игроков, тренеров и судьи."
        )
        # Абсолютный URL карточки для og:image.
        context['og_image'] = self.request.build_absolute_uri(
            reverse('core:match_share_card', args=[match.id])
        )
        schema = {
            "@context": "https://schema.org",
            "@type": "SportsEvent",
            "name": f"{match.home_team.name} vs {match.away_team.name}",
            "startDate": match.start_time.isoformat(),
            # location — город домашней команды.
            "location": {"@type": "Place", "name": match.home_team.city or "Казахстан"},
            "competitor": [
                {"@type": "SportsTeam", "name": match.home_team.name},
                {"@type": "SportsTeam", "name": match.away_team.name},
            ],
            "description": context['meta_description'],
        }
        # Экранируем '</' — строка вставляется через |safe в <script>.
        context['schema_json'] = json.dumps(schema, ensure_ascii=False).replace('</', '<\\/')
        return context

@require_http_methods(["GET"])
def match_events_partial(request, match_id):
    """HTMX-партиал событий матча."""
    match = get_object_or_404(Match, id=match_id)
    events = match.events.select_related(
        'player', 'assist_player', 'player_out'
    ).order_by('minute', 'added_time', 'id')
    return render(request, 'matches/_match_events.html', {
        'match': match,
        'events': events,
    })


def match_action_context(request, match):
    """CTA-флаги матча (голосование/оценка/пульс)."""
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
    """Live-партиал шапки матча (опрос каждые 20 с, пока матч live)."""
    match = get_object_or_404(
        Match.objects.select_related(
            'home_team', 'away_team', 'league', 'season',
            'home_coach', 'away_coach', 'referee',
        ).prefetch_related('home_team__rivals'),  # для match.is_derby
        id=match_id,
    )
    context = match_action_context(request, match)
    context['match'] = match
    return render(request, 'matches/_match_header.html', context)


@require_http_methods(["GET"])
def match_card_partial(request, match_id):
    """Live-поллинг карточки матча в списках (каждые 15 с, только для live).
    attach_card_extras на одном матче — карточка сохраняет все блоки.
    """
    match = get_object_or_404(
        Match.objects.select_related('home_team', 'away_team', 'league', 'season').prefetch_related(
            'aggregate', 'home_team__rivals',
        ),
        id=match_id,
    )
    attach_card_extras([match], request)
    return render(request, 'components/_match_card.html', {'match': match})


# Лимит по user.id.
REACT_TO_MATCH_RATE_LIMIT = 20
REACT_TO_MATCH_RATE_LIMIT_WINDOW_SECONDS = 60


def _reaction_widget_context(request, match):
    return {
        'match': match,
        'counts': reaction_counts(match),
        'my_reaction': user_match_reaction(request.user, match),
    }


@require_POST
def react_to_match(request, match_id):
    """Реакция на завершённый матч («Матч тура» / «Неожиданно» / «Скучно»).
    Возвращает обновлённый HTMX-партиал.
    """
    match = get_object_or_404(Match, id=match_id)
    widget_template = 'matches/_reaction_widget_compact.html'

    if not request.user.is_authenticated:
        # 200, а не 401 — HTMX свапает только 2xx.
        return render(request, 'matches/_reaction_login_prompt_compact.html', {'match': match}, status=200)

    if is_rate_limited(
        f'react_to_match:{request.user.id}', REACT_TO_MATCH_RATE_LIMIT, REACT_TO_MATCH_RATE_LIMIT_WINDOW_SECONDS
    ):
        return HttpResponse(status=429)

    reaction = request.POST.get('reaction')
    if reaction not in dict(MatchReaction.REACTION_CHOICES):
        return HttpResponse(status=400)

    submit_match_reaction(user=request.user, match=match, reaction=reaction)
    # Матч не завершён — форма просто перерисуется.

    return render(request, widget_template, _reaction_widget_context(request, match))