# teams/views.py
from datetime import timedelta

import json

from django.db.models import Avg, Count, Exists, F, OuterRef, Q, Sum
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.decorators.clickjacking import xframe_options_exempt
from django.views.generic import ListView, DetailView
from django.utils import timezone
from core.utils import normalize_kz
from core.models import get_setting
from teams.models import Team, TeamSeason, TeamSeasonStats
from players.models import Player
from matches.models import Match
from aggregates.models import PlayerMatchAggregate, MatchAggregate, TeamMatchAggregate
from lineups.models import MatchLineupPlayer
from aggregates.services import min_votes_for_display, published_q
from aggregates.services import vote_weighted_avg
from seasons.models import Season
import logging

logger = logging.getLogger(__name__)

# Сколько без матчей игрок ещё считается в составе (~15 месяцев).
ROSTER_STALE_THRESHOLD = timedelta(days=450)

class TeamListView(ListView):
    """Список команд."""
    model = Team
    template_name = 'teams/list.html'
    context_object_name = 'teams'
    paginate_by = 20

    def get_paginate_by(self, queryset):
        # Размер страницы — из настроек платформы.
        return get_setting("teams_list_page_size", self.paginate_by)

    def get_queryset(self):
        queryset = Team.objects.all()

        # По умолчанию — команды текущего сезона (TeamSeason). ?season=all — все.
        self.active_season = Season.get_primary_active()
        self.show_all = self.request.GET.get('season') == 'all'
        if self.active_season and not self.show_all:
            queryset = queryset.filter(teamseason__season=self.active_season)

        search = self.request.GET.get('q')
        if search:
            # Поиск через normalize_kz (Кайрат = Қайрат), фильтруем в Python.
            normalized_query = normalize_kz(search)
            matching_ids = [
                t.id for t in Team.objects.only('id', 'name')
                if normalized_query in normalize_kz(t.name)
            ]
            queryset = queryset.filter(id__in=matching_ids)
        # Матчи за текущий сезон; при ?season=all — за всю историю.
        home_season_q = Q(home_matches__season=self.active_season) if self.active_season and not self.show_all else Q()
        away_season_q = Q(away_matches__season=self.active_season) if self.active_season and not self.show_all else Q()
        queryset = queryset.annotate(
            home_matches_count=Count(
                'home_matches',
                filter=Q(home_matches__status='finished') & home_season_q,
                distinct=True
            ),
            away_matches_count=Count(
                'away_matches',
                filter=Q(away_matches__status='finished') & away_season_q,
                distinct=True
            )
        )
        return queryset.order_by('name')
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = 'Все команды — DOPX'
        from aggregates.models import TeamMatchAggregate
        from aggregates.services import entity_ratings
        from teams.models import TeamSeasonStats

        page = list(context['teams'])
        ids = [t.id for t in page]
        season = None if self.show_all else self.active_season
        ratings = entity_ratings(TeamMatchAggregate, 'team_id', ids, season=season)
        standings = ({s.team_id: s for s in TeamSeasonStats.objects.filter(season=season, team_id__in=ids)}
                     if season else {})
        from core.list_rows import team_row

        for t in page:
            t.rating = ratings.get(t.id)
            t.standing = standings.get(t.id)
        context['teams'] = page
        # В сезоне — по месту в таблице, иначе по алфавиту.
        page.sort(key=lambda t: (t.standing.position if t.standing and t.standing.position else 999, t.name))
        context['rows'] = [team_row(t) for t in page]
        context['search_query'] = self.request.GET.get('q', '')
        context['active_season'] = self.active_season
        context['show_all'] = self.show_all
        # Фильтра по городу нет — источник не присылает city для команд.
        return context

class TeamDetailView(DetailView):
    """Страница команды со статистикой за сезон."""
    model = Team
    template_name = 'teams/detail.html'
    context_object_name = 'team'
    
    def get_queryset(self):
        return Team.objects.all()
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        team = self.object
        now = timezone.now()
        
        # Текущий активный сезон
        active_season = Season.objects.filter(is_active=True).first()

        # Выбор сезона — только сезоны, где команда участвовала.
        team_seasons = Season.objects.filter(teamseason__team=team).distinct().order_by('-year')
        season_param = self.request.GET.get('season', '').strip()
        selected_season = team_seasons.filter(year=season_param).first() if season_param else None
        if selected_season is None:
            selected_season = active_season
        current_season = selected_season  # дальше по коду называется current_season

        # Матчи выбранного сезона, только завершённые
        if current_season:
            matches_filter = Q(
                Q(home_team=team) | Q(away_team=team),
                season=current_season,
                status='finished'
            )
        else:
            # Нет активного сезона — все завершённые
            matches_filter = Q(
                Q(home_team=team) | Q(away_team=team),
                status='finished'
            )
        
        # Статистика через aggregate
        stats_data = Match.objects.filter(matches_filter).aggregate(
            played=Count('id'),
            wins=Count('id', filter=(
                (Q(home_team=team) & Q(home_score__gt=F('away_score'))) |
                (Q(away_team=team) & Q(away_score__gt=F('home_score')))
            )),
            draws=Count('id', filter=(
                (Q(home_team=team) & Q(home_score=F('away_score'))) |
                (Q(away_team=team) & Q(away_score=F('home_score')))
            )),
            goals_scored=Sum(
                F('home_score'), filter=Q(home_team=team)
            ) + Sum(
                F('away_score'), filter=Q(away_team=team)
            ),
            goals_conceded=Sum(
                F('away_score'), filter=Q(home_team=team)
            ) + Sum(
                F('home_score'), filter=Q(away_team=team)
            ),
        )
        
        # NULL -> 0
        total_matches = stats_data['played'] or 0
        wins = stats_data['wins'] or 0
        goals_scored = (stats_data['goals_scored'] or 0)
        goals_conceded = (stats_data['goals_conceded'] or 0)
        
        logger.info(f"📊 Team {team.name} stats (season {current_season.year if current_season else 'N/A'}): "
                   f"Matches={total_matches}, Wins={wins}, Scored={goals_scored}, Conceded={goals_conceded}")
        
        season_left = Player.objects.none()
        # Состав команды за выбранный сезон.
        if current_season and not current_season.is_active:
            # Прошлый сезон — кто реально играл за команду в этом сезоне.
            players = Player.objects.filter(
                matchlineupplayer__lineup__team=team,
                matchlineupplayer__lineup__match__season=current_season,
            ).distinct().order_by('number')
        else:
            # Текущий сезон: «состав на сегодня» — кто в клубе сейчас (последний матч или свежий состав Sportmonks);
            # ушедшие, но игравшие за клуб в этом сезоне, — отдельным списком «также выходили».
            # В составе: в клубе сейчас И (играл за него в этом сезоне, ещё нигде не играл или подтверждён свежим
            # составом Sportmonks). Игравший только в прошлых сезонах не попадает.
            current = Q(matchlineupplayer__isnull=True)
            if current_season:
                current |= Q(matchlineupplayer__lineup__team=team, matchlineupplayer__lineup__match__season=current_season)
            if team.squad_synced_at:
                current |= Q(squad_confirmed_at__gte=team.squad_synced_at - timedelta(hours=1))
            players = Player.objects.filter(current, team=team, is_active=True).distinct().order_by('number')
            if current_season:
                season_left = (Player.objects.filter(
                    matchlineupplayer__lineup__team=team,
                    matchlineupplayer__lineup__match__season=current_season,
                ).exclude(team=team).select_related('team').distinct().order_by('last_name'))
        
        # Топ-5 игроков: матч засчитывается команде, за которую сыгран.
        played_for_this_team = MatchLineupPlayer.objects.filter(
            player_id=OuterRef('player_id'),
            lineup__team=team,
            lineup__match_id=OuterRef('match_id'),
        )
        top_players = PlayerMatchAggregate.objects.annotate(
            played_for_this_team=Exists(played_for_this_team)
        ).filter(
            published_q(), played_for_this_team=True, total_votes__gte=min_votes_for_display()
        ).select_related(
            'player',
            'match__home_team', 'match__away_team',
        ).order_by('-performance_score')[:5]
        
        # Средние оценки команды по TeamMatchAggregate (взвешенные), total — сумма голосов.
        team_match_aggs = TeamMatchAggregate.objects.filter(published_q(), team=team).aggregate(
            avg_tactics=vote_weighted_avg('avg_tactics'),
            avg_effort=vote_weighted_avg('avg_effort'),
            avg_organization=vote_weighted_avg('avg_organization'),
            avg_mentality=vote_weighted_avg('avg_mentality'),
            total=Sum('total_votes'),
        )
        team_evals = {
            'avg_tactics': team_match_aggs['avg_tactics'],
            'avg_effort': team_match_aggs['avg_effort'],
            'avg_organization': team_match_aggs['avg_organization'],
            'avg_mentality': team_match_aggs['avg_mentality'],
            'total': team_match_aggs['total'] or 0,
        }
        
        # Последние матчи текущего сезона. Лимит — из настроек платформы.
        recent_matches_limit = get_setting("team_recent_matches_limit", 10)
        if current_season:
            recent_matches = Match.objects.filter(
                Q(home_team=team) | Q(away_team=team),
                season=current_season,
                status='finished'
            ).select_related(
                'home_team',
                'away_team',
                'league',
                'season'
            ).order_by('-start_time')[:recent_matches_limit]
        else:
            recent_matches = Match.objects.filter(
                Q(home_team=team) | Q(away_team=team),
                status='finished'
            ).select_related(
                'home_team',
                'away_team',
                'league',
                'season'
            ).order_by('-start_time')[:recent_matches_limit]
        
        # Ближайшие матчи
        upcoming_matches = Match.objects.filter(
            Q(home_team=team) | Q(away_team=team),
            start_time__gte=now,
            status='scheduled'
        ).select_related(
            'home_team',
            'away_team',
            'league',
            'season'
        ).order_by('start_time')[:5]
        
        # Текущий сезон
        current_season_obj = Season.objects.filter(
            is_active=True,
            teamseason__team=team
        ).first()

        # Место в таблице из TeamSeasonStats; нет строк — досчитываем один раз.
        season_stats = None
        if current_season:
            if not TeamSeasonStats.objects.filter(season=current_season).exists():
                from aggregates.tasks import _recalculate_standings_for_season
                _recalculate_standings_for_season(current_season)
            season_stats = TeamSeasonStats.objects.filter(
                team=team, season=current_season
            ).first()
        total_teams_in_league = None
        if season_stats:
            total_teams_in_league = TeamSeasonStats.objects.filter(
                season=current_season
            ).count()

        # Матч команды, открытый для оценки (CTA).
        votable_match = next((m for m in recent_matches if m.is_voting_open), None)

        is_following = False
        if self.request.user.is_authenticated:
            from users.models import Follow
            is_following = Follow.objects.filter(user=self.request.user, team=team).exists()

        # Индекс настроения клуба: бейдж (сейчас) + график (за сезон).
        from teams.services import (
            compute_mood_trend, compute_mood_series, build_mood_chart,
            find_season_controversial_matches, get_team_form,
        )

        # Форма команды из recent_matches.
        team_form = get_team_form(team, recent_matches)

        mood_trend = compute_mood_trend(team)
        mood_series = compute_mood_series(team)
        # График настроения (teams/services.py::build_mood_chart).
        mood_chart = build_mood_chart(mood_series)
        controversial_matches = (
            find_season_controversial_matches(team, current_season) if current_season else []
        )

        # Состав строками, как список игроков: рейтинг — средняя за выбранный сезон (не случайный матч).
        from aggregates.services import entity_ratings
        from core.list_rows import player_row

        players = list(players)
        squad_ratings = entity_ratings(PlayerMatchAggregate, 'player_id', [p.id for p in players], season=current_season)
        # Травмы и дисквалификации — только для текущего состава, не для прошлых сезонов.
        unavailable = []
        if not (selected_season and not selected_season.is_active):
            from players.models import PlayerSidelined

            periods = (PlayerSidelined.objects.filter(player__team=team, player__is_active=True)
                       .select_related('player').order_by('category', 'end_date', '-start_date'))
            seen = set()
            for s in periods:
                if s.is_current and s.player_id not in seen:
                    seen.add(s.player_id)
                    unavailable.append(s)
        out_by_player = {s.player_id: s for s in unavailable}
        squad_rows = []
        for p in players:
            p.rating = squad_ratings.get(p.id)
            row = player_row(p)
            row['sub'] = row['sub'].split(' · ', 1)[1] if ' · ' in row['sub'] else row['sub']  # клуб и так понятен
            row['meta'] = ''
            if p.id in out_by_player:
                out = out_by_player[p.id]
                row['flag'], row['flag_kind'] = out.get_category_display(), out.category
            squad_rows.append(row)

        context.update({
            'squad_rows': squad_rows,
            'unavailable': unavailable,
            'is_following': is_following,
            'mood_trend': mood_trend,
            'mood_chart': mood_chart,
            'team_form': team_form,
            'controversial_matches': controversial_matches,
            'total_matches': total_matches,
            'wins': wins,
            'goals_scored': goals_scored,
            'goals_conceded': goals_conceded,
            'players': players,
            'season_left_players': season_left,
            'squad_synced_at': team.squad_synced_at,
            'top_players': top_players,
            'team_evals': team_evals,
            'recent_matches': recent_matches,
            'upcoming_matches': upcoming_matches,
            'current_season': current_season_obj,
            'season_stats': season_stats,
            'total_teams_in_league': total_teams_in_league,
            'votable_match': votable_match,
            'team_seasons': team_seasons,
            'active_season': active_season,
            'selected_season': selected_season,
            'page_title': f'{team.name}: оценки болельщиков, форма и рейтинг игроков | DOPX',
        })

        # SEO: описание, логотип для превью, schema.org SportsTeam.
        context['meta_description'] = (
            f"{team.name} на DOPX: как болельщики оценивают команду, игроков и тренера, "
            f"настроение трибун, форма и фан-зона клуба."
        )
        logo = team.logo_display
        if logo:
            context['og_image'] = self.request.build_absolute_uri(logo)
        schema = {
            "@context": "https://schema.org",
            "@type": "SportsTeam",
            "name": team.name,
            "sport": "Football",
            "url": self.request.build_absolute_uri(reverse('teams:detail', args=[team.id])),
            "logo": context.get('og_image'),
            "location": {"@type": "Place", "name": team.city} if team.city else None,
        }
        context['schema_json'] = json.dumps(
            {k: v for k, v in schema.items() if v is not None}, ensure_ascii=False
        ).replace('</', '<\\/')

        # Активная поправка рейтинга — значок «скорректировано».
        rating_correction = getattr(team, 'rating_correction', None)
        if rating_correction is not None and abs(rating_correction.correction) < 0.01:
            rating_correction = None
        context['rating_correction'] = rating_correction
        context['corrected_matches_count'] = TeamMatchAggregate.objects.filter(team=team).filter(
            Q(rating_correction_applied__gte=0.01) | Q(rating_correction_applied__lte=-0.01)
        ).count()

        # Где болеют: города болельщиков (None — мало данных).
        from users.city_stats import team_fan_geography
        context['fan_geography'] = team_fan_geography(team)

        # Фан-зона: рейтинг болельщиков за месяц, главный фанат, соперник.
        from engagement.fanzone import fan_zone
        context['fan_zone'] = fan_zone(team, self.request.user)
        if self.request.user.is_authenticated and is_following:
            from engagement.quests import track
            track(self.request.user, 'fan_zone')
        me = context['fan_zone']['me']
        if me and me['top_percent'] and me['top_percent'] <= 50 and self.request.user.is_profile_public:
            context['fan_brag_image'] = self.request.build_absolute_uri(reverse(
                'engagement:brag_card', args=[self.request.user.username, f'fan_top-{team.id}']
            ))
            context['fan_brag_story'] = reverse('engagement:story_card', args=[self.request.user.username, f'fan_top-{team.id}'])
            context['fan_brag_url'] = self.request.build_absolute_uri(f"{reverse('teams:detail', args=[team.id])}#fan-zone")
            context['fan_brag_text'] = f"Я в топ-{me['top_percent']}% болельщиков {team.name} на DOPX. А ты?"

        # Embed-код виджета.
        widget_url = self.request.build_absolute_uri(reverse('teams:widget', args=[team.id]))
        context['widget_embed_code'] = (
            f'<iframe src="{widget_url}" width="320" height="180" '
            f'style="border:none;border-radius:12px;overflow:hidden" '
            f'title="Рейтинг {team.name} на DOPX"></iframe>'
        )
        players_widget_url = self.request.build_absolute_uri(
            reverse('engagement:team_players_widget', args=[team.id])
        )
        context['players_widget_url'] = players_widget_url
        context['players_widget_embed_code'] = (
            f'<iframe src="{players_widget_url}" width="340" height="460" '
            f'style="border:none;border-radius:12px;overflow:hidden" '
            f'title="Рейтинг игроков {team.name} на DOPX"></iframe>'
        )
        return context


@xframe_options_exempt
def team_rating_widget(request, pk):
    """Виджет команды для чужих сайтов. Рейтинг — среднее TeamMatchAggregate.performance_score.
    @xframe_options_exempt — страница read-only.
    """
    team = get_object_or_404(Team, pk=pk)

    evals_stats = TeamMatchAggregate.objects.filter(published_q(), team=team).aggregate(
        avg_score=vote_weighted_avg('performance_score'), total_votes=Sum('total_votes'),
    )
    total_votes = evals_stats['total_votes'] or 0
    has_enough_votes = total_votes >= min_votes_for_display()

    from partners.services import track_widget_embed_view

    track_widget_embed_view(widget_type="team", entity_id=str(team.id), request=request)

    return render(request, 'widgets/team_rating.html', {
        'team': team,
        'avg_score': round(evals_stats['avg_score'], 1) if evals_stats['avg_score'] is not None else None,
        'total_votes': total_votes,
        'has_enough_votes': has_enough_votes,
    })