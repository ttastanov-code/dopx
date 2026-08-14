# leagues/views.py
from django.views.generic import ListView, DetailView
from django.db.models import Count, Avg, F, Q, Sum
from django.db.models.functions import Coalesce
from django.core.cache import cache  # ✅ Для кэширования
from leagues.models import League
from seasons.models import Season
from matches.models import Match
from teams.models import Team, TeamSeason, TeamSeasonStats
from aggregates.models import MatchAggregate
from players.models import Player
from core.nominations import get_nominations
import logging

logger = logging.getLogger(__name__)


class LeagueListView(ListView):
    """Список всех лиг"""
    model = League
    template_name = 'leagues/list.html'
    context_object_name = 'leagues'
    paginate_by = 20
    
    def get_queryset(self):
        return League.objects.all().order_by('name')
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = 'Все лиги — DOPX'
        return context


class LeagueDetailView(DetailView):
    """Детальная страница лиги с турнирной таблицей"""
    model = League
    template_name = 'leagues/detail.html'
    context_object_name = 'league'
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        league = self.object
        
        # === 1. Сезоны ===
        seasons = Season.objects.filter(
            league=league
        ).annotate(
            match_count=Count('match')
        ).order_by('-year')
        
        # === 2. Активный сезон для таблицы ===
        active_season = seasons.filter(is_active=True).first()

        # === 3. Турнирная таблица ===
        # ИСПРАВЛЕНО: раньше таблица пересчитывалась С НУЛЯ на каждый заход
        # на страницу — цикл по всем командам сезона с 10 Count/Sum-агрегатами
        # НА КАЖДУЮ команду (N+1: 16 команд = 16 отдельных тяжёлых запросов),
        # плюс свой собственный 5-минутный кэш поверх этого. При этом уже
        # существует `TeamSeasonStats` — та же самая таблица, которую заново
        # считает `aggregates/tasks.py::recalculate_season_standings` (Celery
        # Beat, каждые 10 минут) для превью на главной странице. Раньше два
        # разных места (главная и страница лиги) могли в теории показывать
        # слегка разные цифры, если один пересчёт устарел относительно
        # другого — теперь оба читают ОДИН источник правды одним простым
        # индексированным запросом, без ручного кэша здесь (он больше не
        # нужен: TeamSeasonStats и так уже "кэш", обновляемый фоновой
        # задачей).
        standings = []
        if active_season:
            stats_rows = (
                TeamSeasonStats.objects.filter(season=active_season)
                .select_related('team')
                .order_by('position', '-points', '-goal_diff', '-goals_scored')
            )
            standings = [
                {
                    'team_id': str(row.team_id),
                    'team_name': row.team.name,
                    'team_logo_url': row.team.logo_url,
                    'played': row.played,
                    'wins': row.wins,
                    'draws': row.draws,
                    'losses': row.losses,
                    'goals_scored': row.goals_scored,
                    'goals_conceded': row.goals_conceded,
                    'goal_diff': row.goal_diff,
                    'points': row.points,
                }
                for row in stats_rows
            ]

        # === 4. Последние матчи ===
        recent_matches = Match.objects.filter(
            league=league,
            status='finished'  # 🔥 Добавлен фильтр
        ).select_related(
            'home_team', 'away_team', 'season', 'stadium'
        ).order_by('-start_time')[:10]

        # === 5. АНАЛИТИКА СЕЗОНА (новое) ===
        # Раньше в сайдбаре было только "Всего сезонов/Всего матчей/Команд в
        # сезоне" — три бухгалтерские цифры без единой аналитической мысли.
        # Ниже — реальные метрики по данным, которые на сайте УЖЕ собираются
        # (события матчей с 24.02.2026 содержат тип "гол" и передачу;
        # `MatchAggregate` считает "зрелищность"/"напряжение"/"индекс драмы"
        # по оценкам болельщиков — те же метрики, что уже показаны на
        # главной странице, здесь просто в разрезе конкретной лиги/сезона).
        top_scorers = []
        league_mood = None
        most_dramatic_match = None
        best_attack = None
        best_defense = None
        avg_goals_per_match = None

        if active_season:
            cache_key = f'league_{league.id}_season_{active_season.id}_analytics'
            cached = cache.get(cache_key)

            if cached is not None:
                top_scorers = cached['top_scorers']
                league_mood = cached['league_mood']
                most_dramatic_match = cached['most_dramatic_match']
                avg_goals_per_match = cached['avg_goals_per_match']
            else:
                # Бомбардиры: считаем реальные голы из событий матча
                # (events.MatchEvent, event_type='goal'), а не субъективный
                # рейтинг выступления — это разные вещи, и для "бомбардиров"
                # ожидаются именно забитые мячи.
                top_scorers = list(
                    Player.objects.filter(
                        events__match__league=league,
                        events__match__season=active_season,
                        events__event_type='goal',
                    )
                    .select_related('team')
                    .annotate(goals=Count('events'))
                    .order_by('-goals')[:5]
                )

                # "Настроение" сезона: усредняем оценки зрелищности/
                # напряжения/драмы болельщиков по всем оценённым матчам
                # сезона — те же поля, что и в MatchAggregate на главной.
                # Порог total_votes__gte=3 (а не просто ">0"): одна оценка
                # одного болельщика — это его личное мнение, а не "настроение
                # сезона". Тот же принцип статистической значимости, что уже
                # используется по сайту (has_evaluations-гейты на страницах
                # игроков/тренеров, played>=3 для best_attack/best_defense
                # ниже) — секция честно скрывается, пока данных мало, вместо
                # того чтобы выдавать мнение одного человека за общий тренд.
                MIN_VOTES_FOR_MOOD = 3
                mood_agg = MatchAggregate.objects.filter(
                    match__league=league,
                    match__season=active_season,
                    total_votes__gte=MIN_VOTES_FOR_MOOD,
                ).aggregate(
                    avg_entertainment=Avg('avg_entertainment'),
                    avg_tension=Avg('avg_tension'),
                    avg_drama=Avg('drama_index'),
                )
                if mood_agg['avg_entertainment'] is not None:
                    league_mood = {
                        'avg_entertainment': round(mood_agg['avg_entertainment'], 1),
                        'avg_tension': round(mood_agg['avg_tension'], 1),
                        'avg_drama': round(mood_agg['avg_drama'], 1),
                    }

                # Самый "драматичный" матч сезона (по оценке болельщиков) —
                # живая ссылка, а не абстрактная цифра.
                dramatic = (
                    MatchAggregate.objects.filter(
                        match__league=league,
                        match__season=active_season,
                        total_votes__gte=MIN_VOTES_FOR_MOOD,
                    )
                    .select_related('match__home_team', 'match__away_team')
                    .order_by('-drama_index')
                    .first()
                )
                if dramatic:
                    most_dramatic_match = dramatic

                # Среднее число голов за матч — базовая, но реально
                # отсутствовавшая метрика "результативности" сезона.
                goals_agg = Match.objects.filter(
                    league=league, season=active_season, status='finished'
                ).aggregate(
                    total_home=Sum('home_score'),
                    total_away=Sum('away_score'),
                    matches_count=Count('id'),
                )
                if goals_agg['matches_count']:
                    total_goals = (goals_agg['total_home'] or 0) + (goals_agg['total_away'] or 0)
                    avg_goals_per_match = round(total_goals / goals_agg['matches_count'], 2)

                cache.set(cache_key, {
                    'top_scorers': top_scorers,
                    'league_mood': league_mood,
                    'most_dramatic_match': most_dramatic_match,
                    'avg_goals_per_match': avg_goals_per_match,
                }, 300)

            # Лучшая атака/защита — из уже посчитанной турнирной таблицы,
            # без дополнительных запросов. Требуем хотя бы пару сыгранных
            # матчей, чтобы в начале сезона случайный 1 матч не выглядел
            # как "лучшая защита лиги".
            eligible = [row for row in standings if row['played'] >= 3]
            if eligible:
                best_attack = max(eligible, key=lambda r: r['goals_scored'])
                best_defense = min(eligible, key=lambda r: r['goals_conceded'])

        # === НОМИНАЦИИ СЕЗОНА ===
        # Та же витрина, что и на главной (core/nominations.py), но с
        # фильтром по конкретной лиге и активному сезону.
        nominations = get_nominations(league=league, season=active_season) if active_season else []

        context.update({
            'seasons': seasons,
            'active_season': active_season,
            'standings': standings,
            'recent_matches': recent_matches,
            'top_scorers': top_scorers,
            'league_mood': league_mood,
            'most_dramatic_match': most_dramatic_match,
            'best_attack': best_attack,
            'best_defense': best_defense,
            'avg_goals_per_match': avg_goals_per_match,
            'nominations': nominations,
            'page_title': f'{league.name} — DOPX',
        })
        return context