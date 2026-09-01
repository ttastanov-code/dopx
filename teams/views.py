# teams/views.py
from django.db.models import Avg, Count, Exists, F, OuterRef, Q, Sum
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.decorators.clickjacking import xframe_options_exempt
from django.views.generic import ListView, DetailView
from django.utils import timezone
from core.utils import normalize_kz
from teams.models import Team, TeamSeason, TeamSeasonStats
from players.models import Player
from matches.models import Match
from aggregates.models import PlayerMatchAggregate, MatchAggregate, TeamMatchAggregate
from lineups.models import MatchLineupPlayer
from aggregates.services import MIN_VOTES_FOR_DISPLAY
from seasons.models import Season
import logging

logger = logging.getLogger(__name__)

class TeamListView(ListView):
    """Список всех команд"""
    model = Team
    template_name = 'teams/list.html'
    context_object_name = 'teams'
    paginate_by = 20
    
    def get_queryset(self):
        queryset = Team.objects.all()

        # Дефолт: только команды текущего сезона главной лиги (через
        # TeamSeason — реально заполняется на каждом импорте матча, см.
        # parsers/kff/importers.py::import_match_core). Без этого список
        # копил бы вперемешку команды разных сезонов/дивизионов без
        # возможности отличить, кто играет сейчас. Переключатель ?season=all
        # возвращает полный список — нужен, например, чтобы найти вылетевший
        # клуб. См. docs/BACKLOG.md, находка 3.
        self.active_season = Season.get_primary_active()
        self.show_all = self.request.GET.get('season') == 'all'
        if self.active_season and not self.show_all:
            queryset = queryset.filter(teamseason__season=self.active_season)

        search = self.request.GET.get('q')
        if search:
            # normalize_kz — казахские буквы (Қ/Ә/Ұ и т.д.) и их русские
            # "омографы" дают одинаковую строку, "Кайрат" находит
            # "Қайрат" независимо от раскладки (см. core/utils.py).
            # Команд немного (десятки) — дешевле отфильтровать в Python,
            # чем городить SQL TRANSLATE().
            normalized_query = normalize_kz(search)
            matching_ids = [
                t.id for t in Team.objects.only('id', 'name')
                if normalized_query in normalize_kz(t.name)
            ]
            queryset = queryset.filter(id__in=matching_ids)
        queryset = queryset.annotate(
            home_matches_count=Count(
                'home_matches',
                filter=Q(home_matches__status='finished'),
                distinct=True
            ),
            away_matches_count=Count(
                'away_matches',
                filter=Q(away_matches__status='finished'),
                distinct=True
            )
        )
        return queryset.order_by('name')
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = 'Все команды — DOPX'
        context['search_query'] = self.request.GET.get('q', '')
        context['active_season'] = self.active_season
        context['show_all'] = self.show_all
        # Фильтр по городу убран: KFF не присылает city на уровне команды
        # (парсер заполняет city только у Stadium — см. parsers/kff/importers.py),
        # поле Team.city реально всегда пустое, показывать нерабочий
        # dropdown было бы обманом пользователя.
        return context

class TeamDetailView(DetailView):
    """Детальная страница команды со статистикой за текущий сезон."""
    model = Team
    template_name = 'teams/detail.html'
    context_object_name = 'team'
    
    def get_queryset(self):
        return Team.objects.all()
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        team = self.object
        now = timezone.now()
        
        # ✅ ПОЛУЧАЕМ ТЕКУЩИЙ АКТИВНЫЙ СЕЗОН
        current_season = Season.objects.filter(is_active=True).first()
        
        # ✅ ФИЛЬТР МАТЧЕЙ: только текущий сезон + завершённые
        if current_season:
            matches_filter = Q(
                Q(home_team=team) | Q(away_team=team),
                season=current_season,
                status='finished'
            )
        else:
            # Если нет активного сезона, берём все завершённые
            matches_filter = Q(
                Q(home_team=team) | Q(away_team=team),
                status='finished'
            )
        
        # ✅ СТАТИСТИКА ЧЕРЕЗ AGGREGATE (эффективно и правильно)
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
        
        # ✅ ОБРАБОТКА NULL ЗНАЧЕНИЙ
        total_matches = stats_data['played'] or 0
        wins = stats_data['wins'] or 0
        goals_scored = (stats_data['goals_scored'] or 0)
        goals_conceded = (stats_data['goals_conceded'] or 0)
        
        logger.info(f"📊 Team {team.name} stats (season {current_season.year if current_season else 'N/A'}): "
                   f"Matches={total_matches}, Wins={wins}, Scored={goals_scored}, Conceded={goals_conceded}")
        
        # Игроки команды
        players = Player.objects.filter(
            team=team,
            is_active=True
        ).order_by('number')[:25]
        
        # Агрегаты игроков (топ 5). total_votes__gte=MIN_VOTES_FOR_DISPLAY —
        # без этого гейта игрок с одним голосом "10/10 от друга" обходит
        # игрока с честными 40 оценками (тот же баг, что был закрыт для
        # matches/views.py, здесь оставался открытым — продуктовый аудит
        # "доверие к рейтингу", 2026-08-21).
        #
        # НАЙДЕНО (2026-09-01, вопрос пользователя про игрока, сменившего
        # клуб в середине сезона): раньше фильтр был `player__team=team` —
        # то есть "у кого ТЕКУЩАЯ команда — эта", а затем брались ВСЕ
        # PlayerMatchAggregate игрока без разбора, за какую команду был
        # сыгран конкретный матч. Эффект в проде: если игрок блистал в
        # клубе А, а потом перешёл в клуб Б — клуб А молча ТЕРЯЛ его из
        # своего топа (Player.team уже указывает на Б), а клуб Б
        # ПРИСВАИВАЛ себе его лучший матч, сыгранный ещё за А (виджет не
        # проверяет match вообще, только текущую команду игрока). Матч в
        # выдаче показывает только дату, без названий команд — со стороны
        # выглядело так, будто игрок выдал 8.5 именно за Б.
        #
        # Правильный источник "за какую команду сыгран ИМЕННО ЭТОТ матч" —
        # MatchLineupPlayer.lineup.team (см. players/views.py::
        # PlayerDetailView, career_by_season) — не перезаписывается задним
        # числом при трансфере, в отличие от Player.team. Exists-подзапрос
        # ниже проверяет для каждой пары (player, match) из
        # PlayerMatchAggregate: был ли этот игрок в составе ИМЕННО этой
        # команды на ИМЕННО этот матч.
        played_for_this_team = MatchLineupPlayer.objects.filter(
            player_id=OuterRef('player_id'),
            lineup__team=team,
            lineup__match_id=OuterRef('match_id'),
        )
        top_players = PlayerMatchAggregate.objects.annotate(
            played_for_this_team=Exists(played_for_this_team)
        ).filter(
            played_for_this_team=True, total_votes__gte=MIN_VOTES_FOR_DISPLAY
        ).select_related(
            'player',
            'match'
        ).order_by('-performance_score')[:5]
        
        # Оценки команд (средние за карьеру) — 2026-08-23: раньше считались
        # Avg() НАПРЯМУЮ по всей истории TeamEvaluation синхронно на каждый
        # рендер страницы — без веса пользователя, без винзоризации, без
        # защиты от сговора фан-базы. Теперь среднее берётся по уже
        # готовым, взвешенным TeamMatchAggregate.performance_score за
        # каждый матч (см. aggregates/tasks.py::recalculate_team_aggregates)
        # — total считаем как СУММУ голосов по матчам, а не Count() по
        # TeamEvaluation, чтобы гейт MIN_VOTES_FOR_DISPLAY остался
        # осмысленным (число реальных оценок, а не число матчей).
        team_match_aggs = TeamMatchAggregate.objects.filter(team=team).aggregate(
            avg_tactics=Avg('avg_tactics'),
            avg_effort=Avg('avg_effort'),
            avg_organization=Avg('avg_organization'),
            avg_mentality=Avg('avg_mentality'),
            total=Sum('total_votes'),
        )
        team_evals = {
            'avg_tactics': team_match_aggs['avg_tactics'],
            'avg_effort': team_match_aggs['avg_effort'],
            'avg_organization': team_match_aggs['avg_organization'],
            'avg_mentality': team_match_aggs['avg_mentality'],
            'total': team_match_aggs['total'] or 0,
        }
        
        # Последние матчи — ТОЛЬКО текущий сезон + finished
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
            ).order_by('-start_time')[:10]
        else:
            recent_matches = Match.objects.filter(
                Q(home_team=team) | Q(away_team=team),
                status='finished'
            ).select_related(
                'home_team',
                'away_team',
                'league',
                'season'
            ).order_by('-start_time')[:10]
        
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

        # Позиция в турнирной таблице — читает готовую TeamSeasonStats
        # (recalculate_season_standings, Celery Beat каждые 10 минут).
        season_stats = None
        if current_season:
            season_stats = TeamSeasonStats.objects.filter(
                team=team, season=current_season
            ).first()
        total_teams_in_league = None
        if season_stats:
            total_teams_in_league = TeamSeasonStats.objects.filter(
                season=current_season
            ).count()

        # Матч этой команды, который прямо сейчас можно оценить (для CTA
        # в пустом состоянии карточки "Оценки болельщиков") — НЕ просто
        # последний сыгранный, а именно тот, где voting_open_until ещё не
        # истёк, иначе кнопка вела бы на уже закрытое голосование.
        votable_match = next((m for m in recent_matches if m.is_voting_open), None)

        is_following = False
        if self.request.user.is_authenticated:
            from users.models import Follow
            is_following = Follow.objects.filter(user=self.request.user, team=team).exists()

        context.update({
            'is_following': is_following,
            'total_matches': total_matches,
            'wins': wins,
            'goals_scored': goals_scored,
            'goals_conceded': goals_conceded,
            'players': players,
            'top_players': top_players,
            'team_evals': team_evals,
            'recent_matches': recent_matches,
            'upcoming_matches': upcoming_matches,
            'current_season': current_season_obj,
            'season_stats': season_stats,
            'total_teams_in_league': total_teams_in_league,
            'votable_match': votable_match,
            'page_title': f'{team.name} — DOPX',
        })

        # Готовая строка <iframe> для кнопки "Получить embed-код" — тот же
        # паттерн, что у players/views.py::PlayerDetailView.
        widget_url = self.request.build_absolute_uri(reverse('teams:widget', args=[team.id]))
        context['widget_embed_code'] = (
            f'<iframe src="{widget_url}" width="320" height="180" '
            f'style="border:none;border-radius:12px;overflow:hidden" '
            f'title="Рейтинг {team.name} на DOPX"></iframe>'
        )
        return context


@xframe_options_exempt
def team_rating_widget(request, pk):
    """
    Embeddable-виджет команды (продуктовый аудит "канал привлечения",
    2026-08-21) — второй виджет после players:widget. Клубный паблик хочет
    виджет СВОЕЙ команды, не абстрактного игрока, поэтому расширение
    embed-инфраструктуры начинается именно отсюда. @xframe_options_exempt —
    тот же аргумент, что у players/views.py::player_rating_widget: страница
    read-only, без форм и действий, снятие защиты не создаёт поверхность
    для clickjacking.

    Рейтинг — среднее по TeamMatchAggregate.performance_score (уже
    взвешенное и винзоризованное per-match значение, не PlayerMatchAggregate
    — это ОЦЕНКА КОМАНДЫ целиком, а не агрегат по игрокам). 2026-08-23:
    раньше здесь тоже был live Avg() напрямую по TeamEvaluation — та же
    дыра, что была на TeamDetailView, см. её докстринг выше. Гейт
    MIN_VOTES_FOR_DISPLAY тот же порог, что и везде на сайте —
    единообразие важнее точного числа голосов именно на этом виджете.
    """
    team = get_object_or_404(Team, pk=pk)

    evals_stats = TeamMatchAggregate.objects.filter(team=team).aggregate(
        avg_score=Avg('performance_score'), total_votes=Sum('total_votes'),
    )
    total_votes = evals_stats['total_votes'] or 0
    has_enough_votes = total_votes >= MIN_VOTES_FOR_DISPLAY

    from partners.services import track_widget_embed_view

    track_widget_embed_view(widget_type="team", entity_id=str(team.id), request=request)

    return render(request, 'widgets/team_rating.html', {
        'team': team,
        'avg_score': round(evals_stats['avg_score'], 1) if evals_stats['avg_score'] is not None else None,
        'total_votes': total_votes,
        'has_enough_votes': has_enough_votes,
    })