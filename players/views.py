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
from core.utils import normalize_kz
from evaluations.models import PlayerEvaluation
from lineups.models import MatchLineupPlayer
from players.positions import position_label, clean_position_code, LABEL_TO_CODES
from seasons.models import Season
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
        # Дефолт: только игроки команд текущего сезона главной лиги — тот
        # же паттерн, что и TeamListView (см. teams/views.py), через
        # TeamSeason по team_id игрока. ?season=all снимает фильтр. См.
        # docs/BACKLOG.md, находка 3.
        self.active_season = Season.get_primary_active()
        self.show_all = self.request.GET.get('season') == 'all'

        # НАЙДЕНО (2026-09-01, жалоба пользователя): фильтр `is_active=True`
        # был добавлен 2026-08-31 ТОЛЬКО чтобы починить состав команды
        # (teams/views.py — не показывать в текущем ростере игроков, которых
        # KFF больше не видит на странице клуба). Здесь, в общем рейтинге,
        # он же исключал ЛЮБОГО игрока, сменившего клуб/покинувшего КПЛ в
        # середине сезона, — целиком, вместе с его реальными оценками за
        # уже сыгранные матчи (PlayerMatchAggregate никак не завязан на
        # is_active). Продуктовое решение: сезонный рейтинг отражает
        # результативность ЗА СЕЗОН, а не текущий факт трудоустройства —
        # игрок остаётся в рейтинге, is_active используется только для
        # бейджа "покинул клуб/КПЛ" в шаблоне (players/list.html), не для
        # исключения из выдачи.
        queryset = Player.objects.select_related('team').prefetch_related(
            # Prefetch с [:1] — подгружаем только лучший агрегат на игрока, не все
            models.Prefetch(
                'match_aggregates',
                queryset=PlayerMatchAggregate.objects.order_by('-performance_score').only(
                    'id', 'performance_score', 'player_id'
                )[:1],
                to_attr='best_aggregate'
            )
        ).annotate(
            # Через lineup, не через агрегаты — агрегат считается не для всех матчей.
            # 🔥 FIX (2026-08-31, второй проход): раньше считалась ЛЮБАЯ
            # запись в заявке на матч, включая игроков, просидевших весь
            # матч в запасе и не вышедших на замену — см. подробный
            # комментарий в PlayerDetailView.get_context_data выше про
            # is_starting/minute_in. Тут та же логика: matchlineupplayer__is_starting=True
            # ИЛИ matchlineupplayer__minute_in не пусто — то есть реально вышел на поле.
            total_matches=Count(
                'matchlineupplayer__lineup__match',
                filter=Q(matchlineupplayer__lineup__match__status='finished') & (
                    Q(matchlineupplayer__is_starting=True) | Q(matchlineupplayer__minute_in__isnull=False)
                ),
                distinct=True
            )
        )

        if self.active_season and not self.show_all:
            queryset = queryset.filter(team__teamseason__season=self.active_season)

        # Поиск по имени — тот же normalize_kz, что и в поиске команд/
        # тренеров/судей (core/utils.py): "Кайрат" находит "Қайрат" и
        # т.п. независимо от раскладки, которой набирали фамилию.
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
        
        # Фильтр по позиции — значение из <select> теперь ЧЕЛОВЕКОЧИТАЕМАЯ
        # ПОДПИСЬ (label), а не сырой код (см. get_context_data): один
        # label может соответствовать нескольким сырым кодам/регистрам в
        # БД (LABEL_TO_CODES), поэтому фильтруем по всем сразу через
        # __iexact + OR — устойчиво даже если бэкафилл-миграция ещё не
        # прогнана и в БД остались разные регистры одного и того же кода.
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
        # Team не имеет is_active — берём все
        context['teams'] = Team.objects.all()[:20]
        # Список УНИКАЛЬНЫХ подписей, а не сырых кодов — иначе разные
        # варианты регистра одного кода ("AM"/"am") или разные синонимы
        # с одинаковым переводом дали бы дублирующиеся на вид пункты в
        # выпадающем списке (баг, который тут был). Показываем только те
        # подписи, для которых реально есть хотя бы один игрок в БД.
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
    """Детальная страница игрока со статистикой и историей оценок"""
    model = Player
    template_name = 'players/detail.html'
    context_object_name = 'player'
    
    def get_queryset(self):
        return Player.objects.select_related('team').all()
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        player = self.object
        
        # 🔥 FIX (2026-08-31, второй проход): "Матчей сыграно" считался как
        # ЛЮБАЯ запись в MatchLineupPlayer — то есть игрок, просидевший
        # весь матч в запасе и ни разу не вышедший на замену, засчитывался
        # как "сыгравший" наравне со стартовым составом. MatchLineupPlayer
        # различает это через is_starting (в старте) и minute_in (минута
        # выхода на замену — None, если игрок был в заявке, но так и не
        # вышел на поле, см. parsers/kff/importers.py::import_lineups и
        # import_events_and_minutes). Реально "сыграл" = был в старте ИЛИ
        # вышел на замену — просто "был в заявке на матч" сюда не входит.
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
            # Sum, не Count — total_votes всегда не NULL (default=0), Count
            # считал бы число строк агрегата, а не сумму голосов по матчам.
            total_votes=Sum('total_votes'),
        )

        # has_evaluations отдельно от avg-полей — без оценок avg остаётся
        # None (не 0), чтобы шаблон показал "—", а не обманчивый ноль.
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

        # История по сезонам — task #148 (мультисезонность). В отличие от
        # тренеров (docs/BACKLOG.md, находка 4 — KFF физически не хранит
        # историю назначений), у игроков реальная история "команда по
        # сезонам" ВОССТАНОВИМА: MatchLineupPlayer.lineup.team — это
        # команда ИМЕННО НА ТОТ МАТЧ (не перезаписывается задним числом при
        # трансфере), lineup.match.season — сезон конкретного матча.
        # Группируем в Python, а не через .values().annotate() по двум
        # моделям сразу (MatchLineupPlayer + MatchEvent) — так проще
        # корректно посчитать голы на КАЖДЫЙ отрезок сезон+команда (в т.ч.
        # редкий случай трансфера в разгар сезона — тогда игрок получает
        # две отдельные строки, что и есть корректное отображение, а не
        # баг). Данных на игрока — десятки матчей, не тысячи, Python-группировка
        # дешевле, чем городить сложный ORM-запрос ради такой малой выгоды.
        from collections import OrderedDict

        # actually_played (см. выше) — та же защита: не считаем "матчами
        # сыграно" запись о том, что игрок просто был в заявке на матч.
        lineup_entries = MatchLineupPlayer.objects.filter(
            player=player, lineup__match__status='finished'
        ).filter(actually_played).select_related(
            'lineup__match__season__league', 'lineup__team'
        ).order_by('-lineup__match__start_time')

        stints = OrderedDict()  # (season_id, team_id) -> накопитель
        match_ids_by_stint = {}
        for entry in lineup_entries:
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

        # Сортировка: сначала свежие сезоны, внутри сезона — по кол-ву
        # матчей (основной клуб сезона первым, если был трансфер).
        career_by_season = sorted(
            stints.values(),
            key=lambda s: (s['season'].year, s['matches_played']),
            reverse=True,
        )

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

        # Подписан ли текущий пользователь — начальное состояние кнопки
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
            'career_by_season': career_by_season,
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
        # .replace('</', '<\/') — json.dumps НЕ экранирует '</', поэтому имя
        # игрока/команды вида "</script><script>..." могло бы оборвать тег
        # application/ld+json раньше конца JSON (шаблон рендерит эту строку
        # через |safe, см. templates/players/detail.html).
        context['schema_json'] = json.dumps(
            {k: v for k, v in schema.items() if v is not None}, ensure_ascii=False
        ).replace('</', '<\\/')

        # Готовая строка <iframe> для кнопки "Получить embed-код" на странице.
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
        # 🔥 FIX (2026-08-31): та же поправка "реально вышел на поле", что
        # и в PlayerDetailView/PlayerListView — просто быть в заявке на
        # матч (запасным, не вышедшим на замену) не считается "сыгранным".
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

    # Продуктовый аудит "канал привлечения" (2026-08-21): до этого открытия
    # виджета не отслеживались вообще — DOPX не мог сказать партнёру ни
    # "сколько раз ваш паблик показал наш виджет", ни доказать ценность
    # размещения. HTTP_REFERER на iframe-запросе — домен встраивающей
    # страницы (partners/services.py::track_widget_embed_view).
    from partners.services import track_widget_embed_view

    track_widget_embed_view(widget_type="player", entity_id=str(player.id), request=request)

    return render(request, 'widgets/player_rating.html', {
        'player': player,
        'avg_performance': round(stats['avg_performance'], 1) if stats['avg_performance'] is not None else None,
        'total_votes': stats['total_votes'] or 0,
        'has_enough_votes': has_enough_votes,
    })