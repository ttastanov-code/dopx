# core/views.py
from django.shortcuts import render
from django.views.generic import TemplateView
from django.http import HttpResponse
from django.utils import timezone
from django.db.models import Count, Q, F, ExpressionWrapper, IntegerField
from django.template.loader import render_to_string
from matches.models import Match
from teams.models import Team, TeamSeason
from seasons.models import Season
from aggregates.models import MatchAggregate, PlayerMatchAggregate
from evaluations.models import EvaluationSession
import logging

logger = logging.getLogger(__name__)


class HomeView(TemplateView):
    """Главная страница — дашборд"""
    template_name = 'core/home.html'
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        now = timezone.now()
        
        # Последние матчи
        recent_matches = Match.objects.filter(
            status='finished',
            start_time__lte=now
        ).select_related(
            'home_team', 'away_team', 'league', 'season'
        ).prefetch_related('aggregate').order_by('-start_time')[:6]
        
        # Ближайшие матчи
        upcoming_matches = Match.objects.filter(
            status='scheduled',
            start_time__gte=now
        ).select_related(
            'home_team', 'away_team', 'league', 'season'
        ).order_by('start_time')[:4]
        
        # Топ игроки
        top_players = PlayerMatchAggregate.objects.select_related(
            'player', 'player__team'
        ).order_by('-performance_score')[:5]
        
        # Статистика
        stats = {
            'total_matches': Match.objects.count(),
            'active_voting': Match.objects.filter(
                voting_open_until__gte=now,
                status='finished'
            ).count(),
        }
        
        # Метрики
        match_aggs = MatchAggregate.objects.all()
        metrics = {
            'avg_drama': match_aggs.aggregate(Count('id'))['id__count'] and 
                        match_aggs.aggregate(Count('id'))['id__count'] > 0 and 
                        sum(m.drama_index for m in match_aggs[:10]) / min(match_aggs.count(), 10) or 0,
            'avg_entertainment': 0,
        }
        
        active_match_id = None
        if self.request.user.is_authenticated:
            active_session = EvaluationSession.objects.filter(
                user=self.request.user,
                status__in=['started', 'in_progress']
            ).select_related('match').first()
            if active_session:
                active_match_id = active_session.match.id
        
        context.update({
            'recent_matches': recent_matches,
            'upcoming_matches': upcoming_matches,
            'top_players': top_players,
            'stats': stats,
            'metrics': metrics,
            'active_match_id': active_match_id,
            'page_title': 'DOPX — Голос трибун измеряем',
            'now': now,
        })
        return context


def standings_preview(request):
    """
    HTMX partial для превью турнирной таблицы
    Показывает топ-10 команд активного сезона
    """
    # Получаем активный сезон
    season = Season.objects.filter(is_active=True).first()
    
    if not season:
        return HttpResponse('''
            <div class="text-center py-8 opacity-60">
                <i class="ti ti-trophy-off text-3xl mb-2"></i>
                <p class="text-sm">Нет активного сезона</p>
            </div>
        ''')
    
    # Получаем все команды в сезоне
    teams = Team.objects.filter(
        teamseason__season=season,
        is_active=True
    ).distinct()
    
    standings_list = []
    
    for team in teams:
        # Домашние матчи
        home_matches = Match.objects.filter(
            home_team=team,
            season=season,
            status='finished'
        )
        
        # Гостевые матчи
        away_matches = Match.objects.filter(
            away_team=team,
            season=season,
            status='finished'
        )
        
        # Подсчёт статистики
        played = home_matches.count() + away_matches.count()
        
        # Победы (дома: home_score > away_score, в гостях: away_score > home_score)
        wins = (
            home_matches.filter(home_score__gt=F('away_score')).count() +
            away_matches.filter(away_score__gt=F('home_score')).count()
        )
        
        # Ничьи
        draws = (
            home_matches.filter(home_score=F('away_score')).count() +
            away_matches.filter(away_score=F('home_score')).count()
        )
        
        # Поражения
        losses = played - wins - draws
        
        # Забитые голы
        goals_scored = (
            sum(m.home_score or 0 for m in home_matches) +
            sum(m.away_score or 0 for m in away_matches)
        )
        
        # Пропущенные голы
        goals_conceded = (
            sum(m.away_score or 0 for m in home_matches) +
            sum(m.home_score or 0 for m in away_matches)
        )
        
        # Разница мячей
        goal_diff = goals_scored - goals_conceded
        
        # Очки (3 за победу, 1 за ничью)
        points = wins * 3 + draws
        
        standings_list.append({
            'team': team,
            'played': played,
            'wins': wins,
            'draws': draws,
            'losses': losses,
            'goals_scored': goals_scored,
            'goals_conceded': goals_conceded,
            'goal_diff': goal_diff,
            'points': points,
        })
    
    # Сортировка: очки → разница мячей → забитые голы
    standings_list.sort(key=lambda x: (-x['points'], -x['goal_diff'], -x['goals_scored']))
    
    # Берём топ-10 для превью
    standings_list = standings_list[:10]
    
    html = render_to_string('components/_standings_preview.html', {
        'standings': standings_list,
        'season': season,
    })
    
    return HttpResponse(html)


class RulesView(TemplateView):
    """Страница с правилами платформы"""
    template_name = 'core/rules.html'
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = 'Правила платформы — DOPX'
        return context


class ContactsView(TemplateView):
    """Страница с контактами"""
    template_name = 'core/contacts.html'
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = 'Контакты — DOPX'
        return context


def handler_404(request, exception):
    return render(request, 'errors/404.html', status=404)


def handler_500(request):
    return render(request, 'errors/500.html', status=500)