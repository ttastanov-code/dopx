# players/views.py
import json
from collections import Counter

from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.decorators.clickjacking import xframe_options_exempt
from django.views.generic import ListView, DetailView
from django.db.models import Avg, Count, Q, Sum
from django.utils import timezone
from players.models import Player
from teams.models import Team
from aggregates.models import PlayerMatchAggregate
from aggregates.services import MIN_VOTES_FOR_DISPLAY
from aggregates.services import vote_weighted_avg
from core.utils import normalize_kz
from core.models import get_setting
from evaluations.models import PlayerEvaluation
from lineups.models import MatchLineupPlayer
from players.positions import (
    position_label, clean_position_code, LABEL_TO_CODES,
    player_position_breakdown, player_position_display_code,
)
from seasons.models import Season
import logging
import django.db.models as models

logger = logging.getLogger(__name__)

class PlayerListView(ListView):
    """Список игроков с поиском и фильтрами."""
    model = Player
    template_name = 'players/list.html'
    context_object_name = 'players'
    paginate_by = 20

    def get_paginate_by(self, queryset):
        # Размер страницы — из настроек платформы.
        return get_setting("players_list_page_size", self.paginate_by)

    def get_queryset(self):
        # По умолчанию — игроки команд текущего сезона. ?season=all снимает фильтр.
        self.active_season = Season.get_primary_active()
        self.show_all = self.request.GET.get('season') == 'all'

        # «Матчей» — за текущий сезон.
        season_q = (
            Q(matchlineupplayer__lineup__match__season=self.active_season)
            if self.active_season and not self.show_all else Q()
        )

        # is_active не фильтрует рейтинг — только бейдж «покинул клуб».
        queryset = Player.objects.select_related('team').prefetch_related(
            # Prefetch с [:1] — только лучший агрегат на игрока.
            models.Prefetch(
                'match_aggregates',
                queryset=PlayerMatchAggregate.objects.order_by('-performance_score').only(
                    'id', 'performance_score', 'player_id', 'total_votes'
                )[:1],
                to_attr='best_aggregate'
            )
        ).annotate(
            # Матчи через составы: в старте или вышел на замену.
            total_matches=Count(
                'matchlineupplayer__lineup__match',
                filter=Q(matchlineupplayer__lineup__match__status='finished') & season_q & (
                    Q(matchlineupplayer__is_starting=True) | Q(matchlineupplayer__minute_in__isnull=False)
                ),
                distinct=True
            )
        )

        if self.active_season and not self.show_all:
            # Фильтр по команде: игрок играл за неё в активном сезоне,
            # либо у него вообще нет записей в составах (тогда верим Player.team).
            played_this_season_ids = Player.objects.filter(
                matchlineupplayer__lineup__match__season=self.active_season
            ).values_list('id', flat=True)
            never_played_ids = Player.objects.filter(
                team__teamseason__season=self.active_season,
                matchlineupplayer__isnull=True,
            ).values_list('id', flat=True)
            queryset = queryset.filter(
                Q(id__in=played_this_season_ids) | Q(id__in=never_played_ids)
            )

        # Поиск по имени через normalize_kz (Кайрат = Қайрат).
        search = self.request.GET.get('q')
        if search:
            normalized_query = normalize_kz(search)
            matching_ids = [
                p.id for p in Player.objects.only('id', 'first_name', 'last_name')
                if normalized_query in normalize_kz(f"{p.first_name} {p.last_name}")
            ]
            queryset = queryset.filter(id__in=matching_ids)
        
        # Фильтр по команде
        team_id = self.request.GET.get('team')
        if team_id:
            queryset = queryset.filter(team_id=team_id)
        
        # Фильтр по позиции: подпись -> все её коды (LABEL_TO_CODES).
        position_label_selected = self.request.GET.get('position')
        codes = LABEL_TO_CODES.get(position_label_selected, [])
        if codes:
            code_filter = Q()
            for code in codes:
                code_filter |= Q(position__iexact=code)
            queryset = queryset.filter(code_filter)

        return queryset.order_by('last_name', 'first_name')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = 'Все игроки — DOPX'
        context['search_query'] = self.request.GET.get('q', '')
        context['active_season'] = self.active_season
        context['show_all'] = self.show_all
        # Команды выбранного сезона.
        if self.active_season and not self.show_all:
            context['teams'] = Team.objects.filter(
                teamseason__season=self.active_season
            ).distinct().order_by('name')
        else:
            context['teams'] = Team.objects.all().order_by('name')
        # Уникальные подписи позиций, только реально встречающиеся.
        existing_codes = {
            clean_position_code(p)
            for p in Player.objects.exclude(position='').values_list('position', flat=True).distinct()
        }
        context['positions'] = sorted(
            label for label, codes in LABEL_TO_CODES.items()
            if existing_codes & set(codes)
        )
        return context


class PlayerDetailView(DetailView):
    """Страница игрока: статистика и история оценок."""
    model = Player
    template_name = 'players/detail.html'
    context_object_name = 'player'
    
    def get_queryset(self):
        return Player.objects.select_related('team').all()
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        player = self.object
        # Для бейджа «Текущая» в career_by_season.
        active_season = Season.get_primary_active()

        # Сыграл = в старте или вышел на замену.
        from lineups.models import MatchLineupPlayer
        actually_played = Q(is_starting=True) | Q(minute_in__isnull=False)
        actual_matches_count = MatchLineupPlayer.objects.filter(
            player=player,
            lineup__match__status='finished'
        ).filter(actually_played).count()
        
        # Агрегаты игрока по матчам
        aggregates = PlayerMatchAggregate.objects.filter(
            player=player
        ).select_related(
            'match__league',
            'match__season',
            'match__home_team',
            'match__away_team'
        ).order_by('-match__start_time')[:get_setting("player_recent_matches_limit", 20)]

        # Общая статистика
        stats_raw = PlayerMatchAggregate.objects.filter(
            player=player
        ).aggregate(
            avg_performance=vote_weighted_avg('performance_score'),
            avg_risk=vote_weighted_avg('risk_index'),
            avg_maturity=vote_weighted_avg('maturity_score'),
            avg_potential=vote_weighted_avg('avg_potential'),
            evaluated_matches=Count('id', distinct=True),
            # Sum, а не Count — нужна сумма голосов.
            total_votes=Sum('total_votes'),
        )

        # Без оценок avg = None, шаблон покажет «—».
        has_evaluations = stats_raw['evaluated_matches'] > 0

        # Активная поправка рейтинга — показываем значок «скорректировано».
        rating_correction = getattr(player, 'rating_correction', None)
        if rating_correction is not None and abs(rating_correction.correction) < 0.01:
            rating_correction = None
        stats = {
            'avg_performance': round(stats_raw['avg_performance'], 2) if stats_raw['avg_performance'] is not None else None,
            'avg_risk': round(stats_raw['avg_risk'], 2) if stats_raw['avg_risk'] is not None else None,
            'avg_maturity': round(stats_raw['avg_maturity'], 2) if stats_raw['avg_maturity'] is not None else None,
            'avg_potential': round(stats_raw['avg_potential'], 2) if stats_raw['avg_potential'] is not None else None,
            'total_matches': actual_matches_count,  # сыгранные матчи (по составу)
            'evaluated_matches': stats_raw['evaluated_matches'] or 0,  # из них оценено болельщиками
            'total_votes': stats_raw['total_votes'] or 0,
        }

        # Средняя оценка «по статистике» (matches/stat_ratings.py).
        from matches.stat_ratings import average_stat_rating, stat_ratings_for_player

        stats['avg_stat_rating'], stats['stat_rating_matches'] = average_stat_rating(player)
        aggregates = list(aggregates)
        per_match_stat = stat_ratings_for_player(player, [a.match_id for a in aggregates])
        for agg in aggregates:
            agg.stat_rating = per_match_stat.get(agg.match_id)

        # Лучшие матчи игрока
        best_matches = PlayerMatchAggregate.objects.filter(
            player=player
        ).select_related(
            'match__home_team',
            'match__away_team'
        ).order_by('-performance_score')[:5]

        # Команда игрока
        team = player.team

        # История по сезонам: команда берётся из состава на матч, группируем в Python.
        from collections import OrderedDict

        # Только реально сыгранные матчи.
        lineup_entries = MatchLineupPlayer.objects.filter(
            player=player, lineup__match__status='finished'
        ).filter(actually_played).select_related(
            'lineup__match__season__league', 'lineup__team'
        ).order_by('-lineup__match__start_time')

        stints = OrderedDict()  # (season_id, team_id) -> накопитель
        match_ids_by_stint = {}
        for entry in lineup_entries:
            # Первая запись stint'а — самая свежая (сортировка по -start_time).
            match = entry.lineup.match
            season = match.season
            if not season:
                continue
            key = (season.id, entry.lineup.team_id)
            if key not in stints:
                stints[key] = {
                    'season': season,
                    'team': entry.lineup.team,
                    'matches_played': 0,
                    'goals': 0,
                    'latest_match_at': match.start_time,
                }
                match_ids_by_stint[key] = []
            stints[key]['matches_played'] += 1
            match_ids_by_stint[key].append(match.id)

        if stints:
            from events.models import MatchEvent
            goals_by_match = dict(
                MatchEvent.objects.filter(
                    player=player,
                    event_type__in=['goal', 'penalty'],
                    match_id__in=[mid for ids in match_ids_by_stint.values() for mid in ids],
                ).values('match_id').annotate(c=Count('id')).values_list('match_id', 'c')
            )
            for key, match_ids in match_ids_by_stint.items():
                stints[key]['goals'] = sum(goals_by_match.get(mid, 0) for mid in match_ids)

        # Внутри сезона — по дате последнего матча, текущий клуб первым.
        career_by_season = sorted(
            stints.values(),
            key=lambda s: (s['season'].year, s['latest_match_at']),
            reverse=True,
        )

        # Мини-схема позиций по тем же lineup_entries, с учётом стороны (field_position).
        position_counts = Counter(
            player_position_display_code(entry.position, entry.field_position)
            for entry in lineup_entries if entry.position
        )
        position_counts.pop("", None)
        position_breakdown = player_position_breakdown(position_counts)

        # Ближайший матч игрока, который ещё можно оценить (CTA в пустых блоках).
        recent_lineups = MatchLineupPlayer.objects.filter(
            player=player,
            lineup__match__status='finished'
        ).select_related('lineup__match').order_by('-lineup__match__start_time')[:5]
        votable_match = next(
            (lu.lineup.match for lu in recent_lineups if lu.lineup.match.is_voting_open()),
            None
        )

        # Подписан ли пользователь — для кнопки.
        is_following = False
        if self.request.user.is_authenticated:
            from users.models import Follow
            is_following = Follow.objects.filter(user=self.request.user, player=player).exists()

        # Текущая травма/дисквалификация (PlayerSidelined.is_current).
        candidates = list(player.sidelined_periods.order_by('-start_date')[:5])
        active_sidelined = next((s for s in candidates if s.is_current), None)

        context.update({
            'aggregates': aggregates,
            'stats': stats,
            'has_evaluations': has_evaluations,
            'best_matches': best_matches,
            'team': team,
            'active_season': active_season,
            'career_by_season': career_by_season,
            'position_breakdown': position_breakdown,
            'votable_match': votable_match,
            'is_following': is_following,
            'active_sidelined': active_sidelined,
            'rating_correction': rating_correction,
            'page_title': f'{player.first_name} {player.last_name} — DOPX',
        })

        # SEO: meta_description + schema.org Person.
        context['meta_description'] = (
            f"{player.first_name} {player.last_name}"
            + (f" ({team.name})" if team else "")
            + " на DOPX: рейтинг выступлений, риск и потенциал по оценкам болельщиков КПЛ."
        )
        schema = {
            "@context": "https://schema.org",
            "@type": "Person",
            "name": f"{player.first_name} {player.last_name}",
            "jobTitle": "Football Player",
            "affiliation": {"@type": "SportsTeam", "name": team.name} if team else None,
        }
        # Экранируем '</' — строка вставляется через |safe в <script>.
        context['schema_json'] = json.dumps(
            {k: v for k, v in schema.items() if v is not None}, ensure_ascii=False
        ).replace('</', '<\\/')

        # Embed-код виджета.
        widget_url = self.request.build_absolute_uri(
            reverse('players:widget', args=[player.id])
        )
        context['widget_embed_code'] = (
            f'<iframe src="{widget_url}" width="320" height="180" '
            f'style="border:none;border-radius:12px;overflow:hidden" '
            f'title="Рейтинг {player.first_name} {player.last_name} на DOPX"></iframe>'
        )
        return context


class PlayerSeasonRecapView(DetailView):
    """Итоги сезона игрока — отдельная страница для шеринга."""
    model = Player
    template_name = 'players/season_recap.html'
    context_object_name = 'player'

    def get_queryset(self):
        return Player.objects.select_related('team')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        player = self.object

        from seasons.models import Season

        season_id = self.kwargs.get('season_id')
        if season_id:
            season = get_object_or_404(Season, id=season_id)
        else:
            # Без season_id — активный сезон.
            season = Season.objects.filter(is_active=True).select_related('league').first()

        if not season:
            context.update({'season': None})
            return context

        aggregates_qs = PlayerMatchAggregate.objects.filter(player=player, match__season=season)
        stats = aggregates_qs.aggregate(
            avg_performance=vote_weighted_avg('performance_score'),
            total_votes=Sum('total_votes'),
            evaluated_matches=Count('id'),
        )
        # Только реально сыгранные матчи.
        matches_played = MatchLineupPlayer.objects.filter(
            player=player, lineup__match__season=season, lineup__match__status='finished'
        ).filter(
            Q(is_starting=True) | Q(minute_in__isnull=False)
        ).values('lineup__match_id').distinct().count()

        has_enough_votes = (stats['total_votes'] or 0) >= MIN_VOTES_FOR_DISPLAY
        best_match = None
        if has_enough_votes:
            best_match = (
                aggregates_qs.filter(total_votes__gte=MIN_VOTES_FOR_DISPLAY)
                .select_related('match__home_team', 'match__away_team')
                .order_by('-performance_score')
                .first()
            )

        from events.models import MatchEvent

        goals = MatchEvent.objects.filter(
            player=player, event_type__in=['goal', 'penalty'], match__season=season
        ).count()

        avg_performance = round(stats['avg_performance'], 2) if stats['avg_performance'] is not None else None

        context.update({
            'season': season,
            'matches_played': matches_played,
            'evaluated_matches': stats['evaluated_matches'] or 0,
            'avg_performance': avg_performance,
            'has_enough_votes': has_enough_votes,
            'best_match': best_match,
            'goals': goals,
            'page_title': f'Итоги сезона {season.year} — {player.first_name} {player.last_name} — DOPX',
        })
        context['recap_card_url'] = self.request.build_absolute_uri(
            reverse('players:season_recap_card', args=[player.id, season.id])
        )
        return context


def player_season_recap_card(request, pk, season_id):
    """PNG итогов сезона для шеринга."""
    from django.core.files.storage import default_storage
    from django.http import HttpResponseRedirect

    from core.services.share_cards import build_player_season_recap_card
    from seasons.models import Season

    player = get_object_or_404(Player.objects.select_related('team'), pk=pk)
    season = get_object_or_404(Season, pk=season_id)

    stats = PlayerMatchAggregate.objects.filter(player=player, match__season=season).aggregate(
        avg_performance=vote_weighted_avg('performance_score'),
        total_votes=Sum('total_votes'),
    )
    has_enough_votes = (stats['total_votes'] or 0) >= MIN_VOTES_FOR_DISPLAY
    # Только реально сыгранные матчи — как на странице итогов.
    matches_played = MatchLineupPlayer.objects.filter(
        player=player, lineup__match__season=season, lineup__match__status='finished'
    ).filter(
        Q(is_starting=True) | Q(minute_in__isnull=False)
    ).values('lineup__match_id').distinct().count()

    from events.models import MatchEvent

    goals = MatchEvent.objects.filter(
        player=player, event_type__in=['goal', 'penalty'], match__season=season
    ).count()

    relative_path = build_player_season_recap_card(
        player_name=f"{player.first_name} {player.last_name}",
        team_name=player.team.name if player.team else "Без команды",
        season_label=season.year,
        matches_played=matches_played,
        avg_performance=round(stats['avg_performance'], 2) if (has_enough_votes and stats['avg_performance'] is not None) else None,
        goals=goals,
    )
    return HttpResponseRedirect(default_storage.url(relative_path))


@xframe_options_exempt
def player_rating_widget(request, pk):
    """Виджет рейтинга игрока для чужих сайтов (без base.html).
    @xframe_options_exempt — страница read-only, clickjacking не страшен.
    """
    player = get_object_or_404(Player.objects.select_related('team'), pk=pk)

    stats = PlayerMatchAggregate.objects.filter(player=player).aggregate(
        avg_performance=vote_weighted_avg('performance_score'),
        total_votes=Sum('total_votes'),
    )
    has_enough_votes = (stats['total_votes'] or 0) >= MIN_VOTES_FOR_DISPLAY

    # Считаем показы виджета по HTTP_REFERER.
    from partners.services import track_widget_embed_view

    track_widget_embed_view(widget_type="player", entity_id=str(player.id), request=request)

    return render(request, 'widgets/player_rating.html', {
        'player': player,
        'avg_performance': round(stats['avg_performance'], 1) if stats['avg_performance'] is not None else None,
        'total_votes': stats['total_votes'] or 0,
        'has_enough_votes': has_enough_votes,
    })