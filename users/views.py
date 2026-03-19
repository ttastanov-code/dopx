# users/views.py — ИСПРАВЛЕННЫЙ ФАЙЛ
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import login, logout, authenticate
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.views import LoginView as AuthLoginView
from django.contrib import messages
from django.views.generic import CreateView, TemplateView, ListView
from django.urls import reverse_lazy
from django.db.models import Count, Avg, Q
from django.utils import timezone
from users.models import User, UserBadge, UserXP
from users.forms import UserRegistrationForm, UserLoginForm
from evaluations.models import ContextEvaluation, PlayerEvaluation, EvaluationSession
from matches.models import Match
import logging

logger = logging.getLogger(__name__)


class RegisterView(CreateView):
    """Регистрация нового пользователя"""
    model = User
    form_class = UserRegistrationForm
    template_name = 'auth/register.html'
    success_url = reverse_lazy('core:home')

    def form_valid(self, form):
        response = super().form_valid(form)
        user = self.object
        
        # Создаём XP профиль
        UserXP.objects.get_or_create(user=user)
        
        # Выдаём достижение за регистрацию
        UserBadge.objects.get_or_create(
            user=user,
            badge_type='first_evaluation'
        )
        
        login(self.request, user)
        messages.success(self.request, '✅ Аккаунт создан! Добро пожаловать в DOPX.')
        return response

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = 'Регистрация — DOPX'
        return context


class LoginView(AuthLoginView):
    """Вход в аккаунт"""
    template_name = 'auth/login.html'
    authentication_form = UserLoginForm

    def form_valid(self, form):
        response = super().form_valid(form)
        messages.success(self.request, f'👋 С возвращением, {self.request.user.username}!')
        return response

    def get_success_url(self):
        next_url = self.request.GET.get('next')
        if next_url:
            return next_url
        return reverse_lazy('core:home')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = 'Вход — DOPX'
        return context


class LogoutView(LoginRequiredMixin, TemplateView):
    """Выход из аккаунта"""
    def get(self, request, *args, **kwargs):
        logout(request)
        messages.info(self.request, '👋 Вы вышли из аккаунта')
        return redirect('core:home')


class ProfileView(LoginRequiredMixin, TemplateView):
    """Профиль пользователя с полной статистикой"""
    template_name = 'profile/dashboard.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        
        # Статистика оценок
        stats = {
            'total_evaluations': ContextEvaluation.objects.filter(user=user).count(),
            'total_players': PlayerEvaluation.objects.filter(user=user).values('player').distinct().count(),
            'trust_score': round(user.trust_score, 2),
            'trust_level': user.get_trust_level(),
            'evaluation_streak': user.evaluation_streak,
            'total_matches': EvaluationSession.objects.filter(
                user=user, 
                status='completed'
            ).count(),
        }
        
        # Последние оценки
        recent_evaluations = ContextEvaluation.objects.filter(
            user=user
        ).select_related(
            'match__home_team', 
            'match__away_team',
            'match__league'
        ).order_by('-created_at')[:10]
        
        # Достижения
        badges = UserBadge.objects.filter(user=user).order_by('-awarded_at')
        
        # XP
        xp = getattr(user, 'xp', None)
        if not xp:
            xp, _ = UserXP.objects.get_or_create(user=user)
        
        # Активные сессии оценок
        active_sessions = EvaluationSession.objects.filter(
            user=user,
            status__in=['started', 'in_progress']
        ).select_related('match__home_team', 'match__away_team').order_by('-created_at')[:5]
        
        context.update({
            'user': user,
            'stats': stats,
            'recent_evaluations': recent_evaluations,
            'badges': badges,
            'xp': xp,
            'active_sessions': active_sessions,
            'page_title': f'Профиль — {user.username}',
        })
        return context


class UserLeaderboardView(ListView):
    """Таблица лидеров пользователей по Trust Score"""
    model = User
    template_name = 'users/leaderboard.html'
    context_object_name = 'users'
    paginate_by = 20

    def get_queryset(self):
        # ✅ FIX: related_name = 'context_evaluations' (с подчёркиванием!)
        return User.objects.filter(
            is_active=True,
            is_verified=True
        ).annotate(
            eval_count=Count('context_evaluations', distinct=True)  # ✅ с подчёркиванием
        ).filter(
            eval_count__gte=1
        ).order_by('-trust_score', '-eval_count', '-total_evaluations')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = 'Рейтинг пользователей — DOPX'
        context['leaderboard_type'] = 'users'
        return context


class PlayerLeaderboardView(ListView):
    """Таблица лидеров игроков по Performance Score"""
    template_name = 'players/leaderboard.html'
    context_object_name = 'players'
    paginate_by = 20

    def get_queryset(self):
        from players.models import Player
        from aggregates.models import PlayerMatchAggregate
        from django.db.models import Avg, Count
        
        return Player.objects.filter(
            is_active=True
        ).annotate(
            avg_performance=Avg('match_aggregates__performance_score'),
            total_matches=Count('match_aggregates', distinct=True),
            total_votes=Count('match_aggregates__total_votes')
        ).filter(
            total_matches__gte=3 
        ).order_by('-avg_performance')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = 'Рейтинг игроков — DOPX'
        context['leaderboard_type'] = 'players'
        return context