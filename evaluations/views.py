# evaluations/views.py
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib import messages
from django.utils import timezone
from django.views.generic import FormView, TemplateView
from django.db import transaction
from django.core.exceptions import PermissionDenied
from matches.models import Match
from evaluations.models import (
    ContextEvaluation, MatchEvaluation, TeamEvaluation,
    PlayerEvaluation, CoachEvaluation, RefereeEvaluation,
    EvaluationSession
)
from evaluations.forms import (
    ContextEvaluationForm, MatchEvaluationForm,
    TeamEvaluationForm, CoachEvaluationForm, RefereeEvaluationForm
)
from aggregates.tasks import recalculate_all_aggregates_for_match
from lineups.models import MatchLineupPlayer
from aggregates.services import calculate_user_trust_adjustment
from users.models import UserXP, UserBadge
import logging

logger = logging.getLogger(__name__)


class EvaluationWizardMixin:
    """Миксин для управления wizard-оценкой"""
    
    def get_or_create_session(self):
        """Получить или создать сессию оценки"""
        session, created = EvaluationSession.objects.get_or_create(
            user=self.request.user,
            match=self.match,
            defaults={'status': 'started'}
        )
        return session
    
    def update_session(self, session, step_name):
        """Обновить прогресс сессии"""
        if step_name not in session.completed_steps:
            session.completed_steps.append(step_name)
        session.current_step = step_name
        session.status = 'in_progress'
        session.save(update_fields=['completed_steps', 'current_step', 'status', 'updated_at'])
    
    def complete_session(self, session):
        """Завершить сессию"""
        session.status = 'completed'
        session.completed_at = timezone.now()
        session.save(update_fields=['status', 'completed_at', 'updated_at'])
    
    def check_voting_access(self):
        """Проверка доступа к голосованию"""
        now = timezone.now()
        if self.match.voting_open_until < now:
            return False, "Голосование для этого матча закрыто"
        if self.match.status != 'finished':
            return False, "Голосование доступно только для завершённых матчей"
        return True, None


class EvaluateContextView(LoginRequiredMixin, FormView, EvaluationWizardMixin):
    """Шаг 1: Контекст просмотра матча"""
    template_name = 'evaluations/context.html'
    form_class = ContextEvaluationForm

    def dispatch(self, request, *args, **kwargs):
        self.match = get_object_or_404(Match, id=kwargs['match_id'])
        
        # Проверка доступа к голосованию
        can_vote, error_msg = self.check_voting_access()
        if not can_vote:
            messages.error(request, error_msg)
            return redirect('matches:detail', pk=self.match.id)
        
        # Проверка: уже завершённая сессия
        if EvaluationSession.objects.filter(
            user=request.user, match=self.match, status='completed'
        ).exists():
            messages.info(request, 'Вы уже оценили этот матч')
            return redirect('matches:detail', pk=self.match.id)
        
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['match'] = self.match
        return kwargs

    def form_valid(self, form):
        with transaction.atomic():
            session = self.get_or_create_session()
            
            # ✅ FIX: Используем update_or_create вместо save()
            # Это предотвращает IntegrityError при повторной отправке формы
            ContextEvaluation.objects.update_or_create(
                user=self.request.user,
                match=self.match,
                defaults={
                    'supported_team': form.cleaned_data.get('supported_team'),
                    'watched_type': form.cleaned_data.get('watched_type'),
                    'attended_stadium': form.cleaned_data.get('attended_stadium'),
                }
            )
            
            self.update_session(session, 'context')
            messages.success(self.request, '✅ Контекст сохранён')
            
        return redirect('evaluations:teams', match_id=self.match.id)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        session = self.get_or_create_session()
        context.update({
            'match': self.match,
            'page_title': 'Шаг 1: Контекст — DOPX',
            'step': 1,
            'total_steps': 6,
            'progress': session.progress_percentage(),
            'next_step': 'evaluations:teams',
        })
        return context


class EvaluateTeamsView(LoginRequiredMixin, TemplateView, EvaluationWizardMixin):
    """Шаг 2: Оценка команд"""
    template_name = 'evaluations/teams.html'

    def dispatch(self, request, *args, **kwargs):
        self.match = get_object_or_404(Match, id=kwargs['match_id'])
        
        can_vote, error_msg = self.check_voting_access()
        if not can_vote:
            messages.error(request, error_msg)
            return redirect('matches:detail', pk=self.match.id)
        
        session = self.get_or_create_session()
        if 'context' not in session.completed_steps:
            return redirect('evaluations:context', match_id=self.match.id)
        
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        session = self.get_or_create_session()
        
        with transaction.atomic():
            for team in [self.match.home_team, self.match.away_team]:
                prefix = f'team_{team.id}'
                TeamEvaluation.objects.update_or_create(
                    user=request.user,
                    match=self.match,
                    team=team,
                    defaults={
                        'tactics': int(request.POST.get(f'{prefix}_tactics', 5)),
                        'effort': int(request.POST.get(f'{prefix}_effort', 5)),
                        'organization': int(request.POST.get(f'{prefix}_organization', 5)),
                        'mentality': int(request.POST.get(f'{prefix}_mentality', 5)),
                    }
                )
            
            self.update_session(session, 'teams')
            messages.success(request, '✅ Оценки команд сохранены')
        
        return redirect('evaluations:players', match_id=self.match.id)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        session = self.get_or_create_session()
        
        # ✅ FIX: Передаём поля оценки как список словарей (не кортежей!)
        evaluation_fields = [
            {'name': 'tactics', 'label': 'Тактика', 'icon': 'ti-chess'},
            {'name': 'effort', 'label': 'Самоотдача', 'icon': 'ti-flame'},
            {'name': 'organization', 'label': 'Организация', 'icon': 'ti-network'},
            {'name': 'mentality', 'label': 'Менталитет', 'icon': 'ti-brain'},
        ]
        
        context.update({
            'match': self.match,
            'page_title': 'Шаг 2: Команды — DOPX',
            'step': 2,
            'total_steps': 6,
            'progress': session.progress_percentage(),
            'next_step': 'evaluations:players',
            'prev_step': 'evaluations:context',
            'evaluation_fields': evaluation_fields,  # ✅ Передаём в контекст
        })
        return context


class EvaluatePlayersView(LoginRequiredMixin, TemplateView, EvaluationWizardMixin):
    """Шаг 3: Оценка игроков"""
    template_name = 'evaluations/players.html'

    def dispatch(self, request, *args, **kwargs):
        self.match = get_object_or_404(Match, id=kwargs['match_id'])
        
        can_vote, error_msg = self.check_voting_access()
        if not can_vote:
            messages.error(request, error_msg)
            return redirect('matches:detail', pk=self.match.id)
        
        session = self.get_or_create_session()
        if 'teams' not in session.completed_steps:
            return redirect('evaluations:teams', match_id=self.match.id)
        
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        session = self.get_or_create_session()
        lineup_players = MatchLineupPlayer.objects.filter(
            lineup__match=self.match
        ).select_related('player')
        
        count = 0
        with transaction.atomic():
            for lp in lineup_players:
                player = lp.player
                prefix = f'player_{player.id}'
                
                # Сохраняем только если пользователь выбрал оценить игрока
                if request.POST.get(f'{prefix}_evaluate') == 'on':
                    contribution = request.POST.get(f'{prefix}_contribution')
                    risk = request.POST.get(f'{prefix}_risk')
                    potential = request.POST.get(f'{prefix}_potential')
                    
                    if all([contribution, risk, potential]):
                        try:
                            PlayerEvaluation.objects.update_or_create(
                                user=request.user,
                                match=self.match,
                                player=player,
                                defaults={
                                    'contribution': int(contribution),
                                    'risk': int(risk),
                                    'potential': int(potential),
                                }
                            )
                            count += 1
                        except (ValueError, TypeError) as e:
                            logger.warning(f"Ошибка валидации для {player}: {e}")
            
            self.update_session(session, 'players')
            messages.success(request, f'✅ Оценено игроков: {count}')
        
        return redirect('evaluations:coaches', match_id=self.match.id)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        session = self.get_or_create_session()
        context.update({
            'match': self.match,
            'lineup_players': MatchLineupPlayer.objects.filter(
                lineup__match=self.match
            ).select_related('player__team').order_by('is_starting', 'shirt_number'),
            'page_title': 'Шаг 3: Игроки — DOPX',
            'step': 3,
            'total_steps': 6,
            'progress': session.progress_percentage(),
            'next_step': 'evaluations:coaches',
            'prev_step': 'evaluations:teams',
        })
        return context


class EvaluateCoachesView(LoginRequiredMixin, TemplateView, EvaluationWizardMixin):
    """Шаг 4: Оценка тренеров"""
    template_name = 'evaluations/coaches.html'

    def dispatch(self, request, *args, **kwargs):
        self.match = get_object_or_404(Match, id=kwargs['match_id'])
        
        can_vote, error_msg = self.check_voting_access()
        if not can_vote:
            messages.error(request, error_msg)
            return redirect('matches:detail', pk=self.match.id)
        
        session = self.get_or_create_session()
        if 'players' not in session.completed_steps:
            return redirect('evaluations:players', match_id=self.match.id)
        
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        session = self.get_or_create_session()
        
        with transaction.atomic():
            for coach in [self.match.home_coach, self.match.away_coach]:
                if coach:
                    prefix = f'coach_{coach.id}'
                    CoachEvaluation.objects.update_or_create(
                        user=request.user,
                        match=self.match,
                        coach=coach,
                        defaults={
                            'tactics': int(request.POST.get(f'{prefix}_tactics', 5)),
                            'substitutions': int(request.POST.get(f'{prefix}_substitutions', 5)),
                            'game_management': int(request.POST.get(f'{prefix}_management', 5)),
                            'impact': int(request.POST.get(f'{prefix}_impact', 5)),
                        }
                    )
            
            self.update_session(session, 'coaches')
            messages.success(request, '✅ Оценки тренеров сохранены')
        
        return redirect('evaluations:referee', match_id=self.match.id)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        session = self.get_or_create_session()
        coaches = [c for c in [self.match.home_coach, self.match.away_coach] if c]
        context.update({
            'match': self.match,
            'coaches': coaches,
            'page_title': 'Шаг 4: Тренеры — DOPX',
            'step': 4,
            'total_steps': 6,
            'progress': session.progress_percentage(),
            'next_step': 'evaluations:referee',
            'prev_step': 'evaluations:players',
        })
        return context


class EvaluateRefereeView(LoginRequiredMixin, FormView, EvaluationWizardMixin):
    """Шаг 5: Оценка судейства"""
    template_name = 'evaluations/referee.html'
    form_class = RefereeEvaluationForm

    def dispatch(self, request, *args, **kwargs):
        self.match = get_object_or_404(Match, id=kwargs['match_id'])
        
        can_vote, error_msg = self.check_voting_access()
        if not can_vote:
            messages.error(request, error_msg)
            return redirect('matches:detail', pk=self.match.id)
        
        session = self.get_or_create_session()
        if 'coaches' not in session.completed_steps:
            return redirect('evaluations:coaches', match_id=self.match.id)
        
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        session = self.get_or_create_session()
        with transaction.atomic():
            RefereeEvaluation.objects.update_or_create(
                user=self.request.user,
                match=self.match,
                defaults={
                    'influence_score': form.cleaned_data.get('influence_score', 50),
                    'decision_quality': form.cleaned_data.get('decision_quality', 5),
                }
            )
            self.update_session(session, 'referee')
            messages.success(self.request, '✅ Оценка судейства сохранена')
        return redirect('evaluations:match_eval', match_id=self.match.id)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        session = self.get_or_create_session()
        context.update({
            'match': self.match,
            'page_title': 'Шаг 5: Судья — DOPX',
            'step': 5,
            'total_steps': 6,
            'progress': session.progress_percentage(),
            'next_step': 'evaluations:match_eval',
            'prev_step': 'evaluations:coaches',
        })
        return context


class EvaluateMatchFinalView(LoginRequiredMixin, FormView, EvaluationWizardMixin):
    """Шаг 6: Финальная оценка матча"""
    template_name = 'evaluations/match_final.html'
    form_class = MatchEvaluationForm

    def dispatch(self, request, *args, **kwargs):
        self.match = get_object_or_404(Match, id=kwargs['match_id'])
        
        can_vote, error_msg = self.check_voting_access()
        if not can_vote:
            messages.error(request, error_msg)
            return redirect('matches:detail', pk=self.match.id)
        
        session = self.get_or_create_session()
        if 'referee' not in session.completed_steps:
            return redirect('evaluations:referee', match_id=self.match.id)
        
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        session = self.get_or_create_session()
        with transaction.atomic():
            # ✅ 1. Сначала сохраняем оценку матча
            MatchEvaluation.objects.update_or_create(
                user=self.request.user,
                match=self.match,
                defaults={
                    'entertainment': form.cleaned_data.get('entertainment', 5),
                    'tension': form.cleaned_data.get('tension', 5),
                    'turning_point': form.cleaned_data.get('turning_point', False),
                    'fairness': form.cleaned_data.get('fairness', 5),
                }
            )
            
            # ✅ 2. Завершаем сессию
            self.complete_session(session)
            
            # ✅ 3. Обновляем trust_score (ВАЖНО: до начисления XP)
            adjustment = calculate_user_trust_adjustment(self.request.user, self.match)
            old_trust = self.request.user.trust_score
            self.request.user.trust_score = max(0.5, min(2.0, self.request.user.trust_score + adjustment))
            self.request.user.save(update_fields=['trust_score', 'updated_at'])
            
            # ✅ 4. Обновляем статистику оценок (total_evaluations и т.д.)
            self.request.user.update_evaluation_stats()
            
            # ✅ 5. Начисляем XP (ПОСЛЕ обновления статистики!)
            from users.models import UserXP, UserBadge
            xp, _ = UserXP.objects.get_or_create(user=self.request.user)
            xp.add_xp(10)  # +10 XP за завершение оценки
            
            # ✅ 6. Проверяем достижения (ПОСЛЕ начисления XP и обновления статистики)
            from users.services import check_and_award_badges
            awarded_badges = check_and_award_badges(self.request.user)
            
            # ✅ 7. Создаём уведомления о достижениях
            if awarded_badges:
                from notifications.tasks import send_badge_earned_notification
                for badge in awarded_badges:
                    send_badge_earned_notification.delay(
                        user_id=str(self.request.user.id),
                        badge_type=badge.badge_type,
                        badge_name=badge.get_badge_type_display()
                    )
            
            # ✅ 8. Уведомление о повышении уровня (если произошло)
            if xp.level > (getattr(self.request.user, '_old_level', 1)):
                from notifications.tasks import send_level_up_notification
                send_level_up_notification.delay(
                    user_id=str(self.request.user.id),
                    new_level=xp.level,
                    total_xp=xp.total_xp
                )
            
            # ✅ 9. Уведомление об изменении trust_score (если значимое)
            if abs(self.request.user.trust_score - old_trust) >= 0.1:
                from notifications.tasks import send_trust_score_updated_notification
                send_trust_score_updated_notification.delay(
                    user_id=str(self.request.user.id),
                    old_score=round(old_trust, 2),
                    new_score=round(self.request.user.trust_score, 2)
                )
            
            # ✅ 10. Запускаем пересчёт агрегатов (в конце, чтобы не блокировать)
            from aggregates.tasks import recalculate_all_aggregates_for_match
            recalculate_all_aggregates_for_match.delay(str(self.match.id))
            
            messages.success(self.request, '🎉 Оценка завершена! Спасибо за вклад.')
            return redirect('evaluations:complete', match_id=self.match.id)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        session = self.get_or_create_session()
        context.update({
            'match': self.match,
            'page_title': 'Шаг 6: Финал — DOPX',
            'step': 6,
            'total_steps': 6,
            'progress': 100,
            'prev_step': 'evaluations:referee',
        })
        return context


class EvaluationCompleteView(LoginRequiredMixin, TemplateView):
    """Финальная страница завершения оценки"""
    template_name = 'evaluations/complete.html'
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        match_id = self.kwargs.get('match_id')
        if match_id:
            context['match'] = get_object_or_404(Match, id=match_id)
        context['page_title'] = 'Спасибо! — DOPX'
        return context