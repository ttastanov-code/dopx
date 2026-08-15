# players/views.py
import json

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
from evaluations.models import PlayerEvaluation
from lineups.models import MatchLineupPlayer
import logging
import django.db.models as models

logger = logging.getLogger(__name__)

class PlayerListView(ListView):
    """Список всех игроков с поиском и фильтрами"""
    model = Player
    template_name = 'players/list.html'
    context_object_name = 'players'
    paginate_by = 20
    
    def get_queryset(self):
        # ✅ FIX: filter() ДО select_related(), и только для Player
        queryset = Player.objects.filter(is_active=True).select_related('team').prefetch_related(
            # ✅ Prefetch с ограничением: подгружаем только 1 лучший агрегат на игрока
            models.Prefetch(
                'match_aggregates',
                queryset=PlayerMatchAggregate.objects.order_by('-performance_score').only(
                    'id', 'performance_score', 'player_id'
                )[:1],
                to_attr='best_aggregate'  # ✅ Сохраняем в отдельный атрибут
            )
        ).annotate(
            # ✅ FIX: Считаем фактические матчи через lineup, а не агрегаты
            total_matches=Count(
                'matchlineupplayer__lineup__match',
                filter=Q(matchlineupplayer__lineup__match__status='finished'),
                distinct=True
            )
        )
        
        # Поиск по имени
        search = self.request.GET.get('q')
        if search:
            queryset = queryset.filter(
                Q(first_name__icontains=search) |
                Q(last_name__icontains=search)
            )
        
        # Фильтр по команде
        team_id = self.request.GET.get('team')
        if team_id:
            queryset = queryset.filter(team_id=team_id)
        
        # Фильтр по позиции
        position = self.request.GET.get('position')
        if position:
            queryset = queryset.filter(position__icontains=position)
        
        return queryset.order_by('last_name', 'first_name')
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = 'Все игроки — DOPX'
        context['search_query'] = self.request.GET.get('q', '')
        # ✅ FIX: Team не имеет is_active, поэтому просто берём все
        context['teams'] = Team.objects.all()[:20]
        context['positions'] = Player.objects.values_list('position', flat=True).distinct()[:10]
        return context


class PlayerDetailView(DetailView):
    """Детальная страница игрока со статистикой и историей оценок"""
    model = Player
    template_name = 'players/detail.html'
    context_object_name = 'player'
    
    def get_queryset(self):
        return Player.objects.select_related('team').all()
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        player = self.object
        
        # 🔥 FIX: Считаем фактические матчи через MatchLineupPlayer
        from lineups.models import MatchLineupPlayer
        actual_matches_count = MatchLineupPlayer.objects.filter(
            player=player,
            lineup__match__status='finished'
        ).count()
        
        # Агрегаты игрока по матчам
        aggregates = PlayerMatchAggregate.objects.filter(
            player=player
        ).select_related(
            'match__league',
            'match__season',
            'match__home_team',
            'match__away_team'
        ).order_by('-match__start_time')[:20]
        
        # Общая статистика
        stats_raw = PlayerMatchAggregate.objects.filter(
            player=player
        ).aggregate(
            avg_performance=Avg('performance_score'),
            avg_risk=Avg('risk_index'),
            avg_maturity=Avg('maturity_score'),
            avg_potential=Avg('avg_potential'),
            evaluated_matches=Count('id', distinct=True),
            # ✅ ИСПРАВЛЕНО: было Count('total_votes') — это считало количество
            # СТРОК агрегата (у поля total_votes есть default=0, оно никогда
            # не NULL, поэтому Count всегда равнялся числу оценённых матчей,
            # а не реальному числу голосов). Нужна сумма голосов по матчам.
            total_votes=Sum('total_votes'),
        )

        # ✅ ИСПРАВЛЕНО: раньше при отсутствии оценок avg-поля тихо
        # заполнялись нулём и шаблон показывал "0" неотличимо от
        # реального низкого рейтинга (тот же класс бага, что и на
        # странице команды/главной). Теперь отдельно храним признак
        # has_evaluations, а сами avg-поля остаются None, если оценок
        # нет — шаблон показывает "—" вместо обманчивого нуля.
        has_evaluations = stats_raw['evaluated_matches'] > 0
        stats = {
            'avg_performance': round(stats_raw['avg_performance'], 2) if stats_raw['avg_performance'] is not None else None,
            'avg_risk': round(stats_raw['avg_risk'], 2) if stats_raw['avg_risk'] is not None else None,
            'avg_maturity': round(stats_raw['avg_maturity'], 2) if stats_raw['avg_maturity'] is not None else None,
            'avg_potential': round(stats_raw['avg_potential'], 2) if stats_raw['avg_potential'] is not None else None,
            'total_matches': actual_matches_count,  # реально сыгранные матчи (по составу)
            'evaluated_matches': stats_raw['evaluated_matches'] or 0,  # из них оценено болельщиками
            'total_votes': stats_raw['total_votes'] or 0,
        }

        # Лучшие матчи игрока
        best_matches = PlayerMatchAggregate.objects.filter(
            player=player
        ).select_related(
            'match__home_team',
            'match__away_team'
        ).order_by('-performance_score')[:5]

        # Команда игрока
        team = player.team

        # НОВОЕ: ближайший сыгранный матч этого игрока, который ещё можно
        # оценить — используется для CTA в пустых состояниях ("История
        # выступлений" / "Лучшие матчи"), чтобы не просто прятать карточки,
        # а вести пользователя к действию, как на странице команды.
        recent_lineups = MatchLineupPlayer.objects.filter(
            player=player,
            lineup__match__status='finished'
        ).select_related('lineup__match').order_by('-lineup__match__start_time')[:5]
        votable_match = next(
            (lu.lineup.match for lu in recent_lineups if lu.lineup.match.is_voting_open()),
            None
        )

        # Follow-граф (продуктовый аудит, раздел 5b) — подписан ли ТЕКУЩИЙ
        # пользователь на этого игрока, для начального состояния кнопки
        # (templates/users/_follow_button.html).
        is_following = False
        if self.request.user.is_authenticated:
            from users.models import Follow
            is_following = Follow.objects.filter(user=self.request.user, player=player).exists()

        context.update({
            'aggregates': aggregates,
            'stats': stats,
            'has_evaluations': has_evaluations,
            'best_matches': best_matches,
            'team': team,
            'votable_match': votable_match,
            'is_following': is_following,
            'page_title': f'{player.first_name} {player.last_name} — DOPX',
        })

        # SEO: meta_description + schema.org (Person) — см. аналогичный
        # комментарий в matches/views.py::MatchDetailView про json.dumps
        # вместо ручной интерполяции в <script>.
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
        context['schema_json'] = json.dumps({k: v for k, v in schema.items() if v is not None}, ensure_ascii=False)

        # Embed-код для виджета (продуктовый аудит, раздел 5 "Рост"):
        # готовая строка <iframe>, которую можно скопировать одной кнопкой —
        # см. kнопку "Получить embed-код" ниже на странице.
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
    """
    Продуктовый аудит, раздел 5d ("Автогенерируемый season recap"):
    "DOPX Wrapped" для одного игрока — сводка за сезон на отдельной,
    шерабельной странице (в отличие от общей истории на players:detail,
    которая показывает ВСЕ сезоны сразу).
    """
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
            # Без явного season_id в URL — сезон по умолчанию: текущий
            # активный (см. Season.is_active, авто-переключается парсером).
            season = Season.objects.filter(is_active=True).select_related('league').first()

        if not season:
            context.update({'season': None})
            return context

        aggregates_qs = PlayerMatchAggregate.objects.filter(player=player, match__season=season)
        stats = aggregates_qs.aggregate(
            avg_performance=Avg('performance_score'),
            total_votes=Sum('total_votes'),
            evaluated_matches=Count('id'),
        )
        matches_played = MatchLineupPlayer.objects.filter(
            player=player, lineup__match__season=season, lineup__match__status='finished'
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
    """PNG-версия season recap для шеринга (og:image/Telegram/WhatsApp)."""
    from django.core.files.storage import default_storage
    from django.http import HttpResponseRedirect

    from core.services.share_cards import build_player_season_recap_card
    from seasons.models import Season

    player = get_object_or_404(Player.objects.select_related('team'), pk=pk)
    season = get_object_or_404(Season, pk=season_id)

    stats = PlayerMatchAggregate.objects.filter(player=player, match__season=season).aggregate(
        avg_performance=Avg('performance_score'),
        total_votes=Sum('total_votes'),
    )
    has_enough_votes = (stats['total_votes'] or 0) >= MIN_VOTES_FOR_DISPLAY
    matches_played = MatchLineupPlayer.objects.filter(
        player=player, lineup__match__season=season, lineup__match__status='finished'
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
    """
    Продуктовый аудит, раздел 5 ("Рост"): embeddable-виджет рейтинга
    игрока для сторонних сайтов (фан-паблики, клубные страницы). Отдельный
    минимальный шаблон БЕЗ base.html (без шапки/футера/меню сайта) — внутри
    <iframe> шириной 300-320px сайт-обёртка DOPX выглядела бы абсурдно.

    `@xframe_options_exempt`: глобальный `XFrameOptionsMiddleware`
    (dopx/settings.py) по умолчанию ставит `X-Frame-Options: SAMEORIGIN`
    на КАЖДЫЙ ответ — правильная защита от clickjacking для всего сайта
    (форм оценки, входа и т.д.), но именно ЭТА страница должна открываться
    в чужом origin по определению. Виджет строго read-only (нет форм,
    кнопок действия, ссылка "Подробнее на DOPX" ведёт на обычную страницу
    игрока) — снятие защиты не создаёт поверхность для clickjacking.
    """
    player = get_object_or_404(Player.objects.select_related('team'), pk=pk)

    stats = PlayerMatchAggregate.objects.filter(player=player).aggregate(
        avg_performance=Avg('performance_score'),
        total_votes=Sum('total_votes'),
    )
    has_enough_votes = (stats['total_votes'] or 0) >= MIN_VOTES_FOR_DISPLAY

    return render(request, 'widgets/player_rating.html', {
        'player': player,
        'avg_performance': round(stats['avg_performance'], 1) if stats['avg_performance'] is not None else None,
        'total_votes': stats['total_votes'] or 0,
        'has_enough_votes': has_enough_votes,
    })