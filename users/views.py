# users/views.py — ИСПРАВЛЕННЫЙ ФАЙЛ
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import login, logout, authenticate, update_session_auth_hash
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.views import LoginView as AuthLoginView, PasswordChangeView, PasswordResetView, PasswordResetDoneView, PasswordResetConfirmView, PasswordResetCompleteView
from django.contrib import messages
from django.views.generic import CreateView, TemplateView, ListView, UpdateView, FormView
from django.urls import reverse_lazy
from django.db.models import Count, Avg, Q
from django.utils import timezone
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.conf import settings
from users.models import User, UserBadge, UserXP
from users.forms import UserRegistrationForm, UserLoginForm, UserProfileForm, CustomPasswordChangeForm, CustomPasswordResetForm, NotificationSettingsForm
from evaluations.models import ContextEvaluation, PlayerEvaluation, EvaluationSession
from matches.models import Match
import logging
import json

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
        # Обработка "Запомнить меня"
        remember_me = self.request.POST.get('remember')
        if not remember_me:
            self.request.session.set_expiry(0)  # Сессия истекает при закрытии браузера
        else:
            self.request.session.set_expiry(1209600)  # 2 недели
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

class ProfileEditView(LoginRequiredMixin, UpdateView):
    """Редактирование профиля"""
    model = User
    form_class = UserProfileForm
    template_name = 'users/profile_edit.html'
    success_url = reverse_lazy('users:profile')
    
    def get_object(self):
        return self.request.user
    
    def form_valid(self, form):
        # Обработка удаления аватарки
        if form.cleaned_data.get('delete_avatar'):
            if self.object.avatar:
                self.object.avatar.delete(save=False)
                self.object.avatar = None
        
        messages.success(self.request, '✅ Профиль успешно обновлён')
        return super().form_valid(form)
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = 'Редактирование профиля — DOPX'
        return context

class PasswordChangeViewCustom(LoginRequiredMixin, FormView):
    """Изменение пароля"""
    template_name = 'users/password_change.html'
    form_class = CustomPasswordChangeForm
    success_url = reverse_lazy('users:profile')
    
    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['user'] = self.request.user
        return kwargs
    
    def form_valid(self, form):
        form.save()
        update_session_auth_hash(self.request, form.user)
        messages.success(self.request, '✅ Пароль успешно изменён')
        return super().form_valid(form)
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = 'Изменить пароль — DOPX'
        return context

class PasswordResetViewCustom(PasswordResetView):
    """Сброс пароля"""
    template_name = 'auth/password_reset.html'
    email_template_name = 'emails/password_reset_email.txt'  # Текстовая версия
    html_email_template_name = 'emails/password_reset_email.html'  # HTML версия
    subject_template_name = 'emails/password_reset_subject.txt'
    success_url = reverse_lazy('users:password_reset_done')
    form_class = CustomPasswordResetForm
    
    def form_valid(self, form):
        messages.success(self.request, '✅ Инструкция по сбросу пароля отправлена на email')
        return super().form_valid(form)

class PasswordResetDoneViewCustom(PasswordResetDoneView):
    """Страница после запроса сброса пароля"""
    template_name = 'auth/password_reset_done.html'

class PasswordResetConfirmViewCustom(PasswordResetConfirmView):
    """Подтверждение сброса пароля"""
    template_name = 'auth/password_reset_confirm.html'
    success_url = reverse_lazy('users:password_reset_complete')

class PasswordResetCompleteViewCustom(PasswordResetCompleteView):
    """Завершение сброса пароля"""
    template_name = 'auth/password_reset_complete.html'

class NotificationSettingsView(LoginRequiredMixin, FormView):
    """Настройки уведомлений"""
    template_name = 'users/notification_settings.html'
    form_class = NotificationSettingsForm
    success_url = reverse_lazy('users:profile')
    
    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['user'] = self.request.user
        return kwargs
    
    def form_valid(self, form):
        # Сохраняем настройки в JSON поле
        settings_data = {
            'email_match_finished': form.cleaned_data.get('email_match_finished', False),
            'email_voting_open': form.cleaned_data.get('email_voting_open', False),
            'email_voting_closing': form.cleaned_data.get('email_voting_closing', False),
            'email_top_performance': form.cleaned_data.get('email_top_performance', False),
            'email_system': form.cleaned_data.get('email_system', False),
        }
        # Сохраняем в профиль пользователя
        self.request.user.notification_settings = settings_data
        self.request.user.save(update_fields=['notification_settings'])
        messages.success(self.request, '✅ Настройки уведомлений сохранены')
        return super().form_valid(form)
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = 'Настройки уведомлений — DOPX'
        return context

class UserLeaderboardView(ListView):
    """Таблица лидеров пользователей по Trust Score"""
    model = User
    template_name = 'users/leaderboard.html'
    context_object_name = 'users'
    paginate_by = 20
    
    def get_queryset(self):
        return User.objects.filter(
            is_active=True,
            is_verified=True
        ).annotate(
            eval_count=Count('context_evaluations', distinct=True)
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
        from django.db.models import Avg, Count, Sum, Q
        return Player.objects.filter(
            is_active=True
        ).annotate(
            avg_performance=Avg('match_aggregates__performance_score'),
            total_matches=Count('match_aggregates', distinct=True),
            total_votes=Sum('match_aggregates__total_votes')
        ).filter(
            avg_performance__isnull=False,
            total_matches__gte=1
        ).order_by('-avg_performance')
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = 'Рейтинг игроков — DOPX'
        context['leaderboard_type'] = 'players'
        return context