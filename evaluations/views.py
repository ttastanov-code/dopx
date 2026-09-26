# evaluations/views.py
"""6-шаговый вайзард оценки матча.

XP начисляется по шагам (контекст +2, команды +2, игроки до +3 пропорционально
оценённым, тренеры +1, судья +1, финал +1) и умножается на xp_multiplier().
Достижения проверяются асинхронно после коммита. Финальный шаг ставит
антифрод-проверку скорости заполнения. Trust score пересчитывается после
закрытия голосования (users.tasks.settle_trust_scores_task).
После завершения оценку изменить нельзя.
"""
from __future__ import annotations

from functools import partial

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core import signing
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views.generic import FormView, TemplateView

from aggregates.tasks import recalculate_all_aggregates_for_match
from analytics.models import EventName
from analytics.services import track_event
from core.utils import get_client_ip
from evaluations.policies import EvaluationPolicyError, assert_voting_open
from evaluations.forms import (
    CoachEvaluationForm,
    ContextEvaluationForm,
    MatchEvaluationForm,
    PlayerEvaluationForm,
    RefereeEvaluationForm,
    TeamEvaluationForm,
)
from evaluations.models import (
    CoachEvaluation,
    ContextEvaluation,
    EvaluationSession,
    MatchEvaluation,
    PlayerEvaluation,
    RefereeEvaluation,
    TeamEvaluation,
)
from events.models import MatchEvent
from lineups.models import MatchLineupPlayer
from matches.models import Match, MatchPlayerStatistics
from notifications.models import Notification
from notifications.tasks import send_level_up_notification, send_push_task
from users.models import UserXP, is_email_verified
from users.tasks import check_and_award_badges_task, flag_suspicious_wizard_speed_task

import logging

logger = logging.getLogger(__name__)

# XP за шаги вайзарда (в сумме 10), умножается на xp_multiplier().
XP_CONTEXT_STEP = 2
XP_TEAMS_STEP = 2
XP_PLAYERS_STEP_MAX = 3
XP_COACHES_STEP = 1
XP_REFEREE_STEP = 1
XP_FINAL_STEP = 1

# Режим «Быстро»: сколько игроков на команду раскрыто по умолчанию.
# См. docs/adr/0006-quick-full-evaluation-mode.md.
KEY_PLAYERS_PER_SIDE = 3

# Пороги «заметности» игрока по статистике матча — только для выбора, чья карточка
# раскрыта в режиме «Быстро». Баллы статистика не предзаполняет.
STATS_KEY_PLAYER_SHOTS_ON_TARGET = 2
STATS_KEY_PLAYER_SHOTS = 4
STATS_KEY_PLAYER_SAVES = 3


def _pluralize_ru(n: int, one: str, few: str, many: str) -> str:
    """Русское склонение по числу (1 / 2-4 / 5+). Django |pluralize так не умеет."""
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not (12 <= n % 100 <= 14):
        return few
    return many


def _player_stats_badges(stats: "MatchPlayerStatistics") -> list[str]:
    """До 3 строк справочной статистики для карточки игрока: сейвы, удары, фолы."""
    badges = []
    if stats.saves:
        badges.append(f"{stats.saves} {_pluralize_ru(stats.saves, 'сейв', 'сейва', 'сейвов')}")
    if stats.shots:
        word = _pluralize_ru(stats.shots, 'удар', 'удара', 'ударов')
        extra = f" ({stats.shots_on_target} в створ)" if stats.shots_on_target else ""
        badges.append(f"{stats.shots} {word}{extra}")
    if stats.fouls:
        badges.append(f"{stats.fouls} {_pluralize_ru(stats.fouls, 'фол', 'фола', 'фолов')}")
    return badges


def _wizard_xp_key(request) -> str:
    # Ключ по матчу: брошенный вайзард другого матча не попадает в сводку.
    return f"wizard_xp_earned:{request.resolver_match.kwargs['match_id']}"


def _track_wizard_xp(request, amount: int) -> None:
    """Копит зачисленный XP за текущее прохождение — финальный шаг показывает сумму."""
    if amount <= 0:
        return
    key = _wizard_xp_key(request)
    request.session[key] = request.session.get(key, 0) + amount


def _award_step_xp(request, base_amount: float) -> dict | None:
    """Начисляет XP за шаг с учётом xp_multiplier(); в сводку идёт реально зачисленное."""
    if base_amount <= 0:
        return None
    user = request.user
    xp, _created = UserXP.objects.get_or_create(user=user)
    result = xp.add_xp(base_amount * user.xp_multiplier())
    _track_wizard_xp(request, result["new_total_xp"] - result["old_total_xp"])
    return result


def _touched_fields(post_data, field_names: list) -> list:
    """Какие ползунки пользователь реально тронул (скрытые поля <name>__touched от JS).
    Если таких полей нет вообще (JS не работал) — считаем тронутыми все.
    См. docs/adr/0005-anti-noise-touched-tracking.md.
    """
    js_ran = any(f'{name}__touched' in post_data for name in field_names)
    if not js_ran:
        return list(field_names)
    return [name for name in field_names if post_data.get(f'{name}__touched') == '1']


# Шаг -> имя URL, куда вернуть, если он не пройден.
STEP_URL_NAMES = {
    'context': 'context', 'teams': 'teams', 'players': 'players',
    'coaches': 'coaches', 'referee': 'referee',
}


class EvaluationWizardMixin:
    def require_login_or_redirect(self, request):
        """Редирект на логин для анонима до обращения к сессии в dispatch().
        LoginRequiredMixin срабатывает позже, а запрос с AnonymousUser падает.
        Неподтверждённая почта (например, после смены email) — голосовать нельзя.
        """
        if not request.user.is_authenticated:
            messages.info(request, 'Войдите, чтобы оценить матч.')
            return self.handle_no_permission()
        if not is_email_verified(request.user):
            messages.warning(request, 'Подтвердите почту по ссылке из письма, чтобы оценивать матчи.')
            return redirect('matches:detail', pk=request.resolver_match.kwargs['match_id'])
        return None

    def prepare_step(self, request, match_id, required_step: str | None):
        """Общие проверки шага: вход, почта, окно голосования, порядок шагов, завершённость.
        Возвращает redirect или None.
        """
        redirect_response = self.require_login_or_redirect(request)
        if redirect_response is not None:
            return redirect_response
        self.match = get_object_or_404(Match, id=match_id)
        can_vote, error_msg = self.check_voting_access()
        if not can_vote:
            messages.error(request, error_msg)
            return redirect('matches:detail', pk=self.match.id)
        session = self.get_or_create_session()
        # Завершённую оценку не меняем: иначе можно подстроиться под общий рейтинг задним числом.
        if session.status == 'completed':
            messages.info(request, 'Вы уже оценили этот матч.')
            return redirect('matches:detail', pk=self.match.id)
        if required_step and required_step not in session.completed_steps:
            return redirect(f'evaluations:{STEP_URL_NAMES[required_step]}', match_id=self.match.id)
        return None

    def get_or_create_session(self):
        session, created = EvaluationSession.objects.get_or_create(
            user=self.request.user, match=self.match, defaults={'status': 'started'}
        )
        return session

    def update_session(self, session, step_name):
        if step_name not in session.completed_steps:
            session.completed_steps.append(step_name)
            session.current_step = step_name
            session.status = 'in_progress'
            session.save(update_fields=['completed_steps', 'current_step', 'status', 'updated_at'])

    def complete_session(self, session):
        session.status = 'completed'
        session.completed_at = timezone.now()
        session.ip_address = get_client_ip(self.request)
        session.save(update_fields=['status', 'completed_at', 'ip_address', 'updated_at'])

    def check_voting_access(self):
        try:
            assert_voting_open(self.match)
        except EvaluationPolicyError as e:
            return False, str(e)
        return True, None


class EvaluateContextView(LoginRequiredMixin, FormView, EvaluationWizardMixin):
    template_name = 'evaluations/context.html'
    form_class = ContextEvaluationForm

    def dispatch(self, request, *args, **kwargs):
        redirect_response = self.prepare_step(request, kwargs['match_id'], None)
        if redirect_response is not None:
            return redirect_response
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['match'] = self.match
        return kwargs

    def form_valid(self, form):
        with transaction.atomic():
            # select_for_update() — защита от двойного сабмита.
            # См. docs/adr/0015-evaluation-wizard-concurrency-and-reward-delivery.md.
            session = self.get_or_create_session()
            session = EvaluationSession.objects.select_for_update().get(pk=session.pk)
            is_new_step = 'context' not in session.completed_steps
            ContextEvaluation.objects.update_or_create(
                user=self.request.user, match=self.match,
                defaults={
                    'supported_team': form.cleaned_data.get('supported_team'),
                    'watched_type': form.cleaned_data.get('watched_type'),
                    'attended_stadium': form.cleaned_data.get('attended_stadium'),
                }
            )
            # Режим «Быстро/Подробно» выбирается на первом шаге и хранится в сессии оценки.
            requested_mode = self.request.POST.get('eval_mode')
            if requested_mode in dict(EvaluationSession.MODE_CHOICES) and session.mode != requested_mode:
                session.mode = requested_mode
                session.save(update_fields=['mode', 'updated_at'])
            self.update_session(session, 'context')
            if is_new_step:
                _award_step_xp(self.request, XP_CONTEXT_STEP)
        messages.success(self.request, 'Контекст сохранён.')
        return redirect('evaluations:teams', match_id=self.match.id)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        session = self.get_or_create_session()
        context.update({
            'match': self.match, 'page_title': 'Шаг 1: Контекст — DOPX',
            'step': 1, 'total_steps': 6, 'progress': session.progress_percentage(),
            'next_step': 'evaluations:teams', 'mode': session.mode,
        })
        return context


class EvaluateTeamsView(LoginRequiredMixin, TemplateView, EvaluationWizardMixin):
    template_name = 'evaluations/teams.html'

    def dispatch(self, request, *args, **kwargs):
        redirect_response = self.prepare_step(request, kwargs['match_id'], 'context')
        if redirect_response is not None:
            return redirect_response
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        # Значения через форму — валидация 1..10.
        form = TeamEvaluationForm(request.POST, match=self.match)
        if not form.is_valid():
            messages.error(request, 'Проверьте оценки команд. Что-то введено некорректно.')
            return self.render_to_response(self.get_context_data(form=form))

        session = self.get_or_create_session()
        with transaction.atomic():
            # Блокировка сессии от двойного POST.
            session = EvaluationSession.objects.select_for_update().get(pk=session.pk)
            is_new_step = 'teams' not in session.completed_steps
            rated_teams = 0
            for team in [self.match.home_team, self.match.away_team]:
                prefix = f'team_{team.id}'
                criteria = [f'{prefix}_tactics', f'{prefix}_effort', f'{prefix}_organization', f'{prefix}_mentality']
                # Ни один ползунок не тронут — оценку команды не создаём.
                if not _touched_fields(request.POST, criteria):
                    continue
                TeamEvaluation.objects.update_or_create(
                    user=request.user, match=self.match, team=team,
                    defaults={
                        'tactics': form.cleaned_data[f'{prefix}_tactics'],
                        'effort': form.cleaned_data[f'{prefix}_effort'],
                        'organization': form.cleaned_data[f'{prefix}_organization'],
                        'mentality': form.cleaned_data[f'{prefix}_mentality'],
                    }
                )
                rated_teams += 1
            self.update_session(session, 'teams')
            if is_new_step:
                _award_step_xp(request, XP_TEAMS_STEP)
        if rated_teams:
            messages.success(request, f'Оценки команд сохранены: {rated_teams} из 2.')
        else:
            messages.info(request, 'Команды пропущены — ни один критерий не был отмечен.')
        return redirect('evaluations:players', match_id=self.match.id)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        session = self.get_or_create_session()
        evaluation_fields = [
            {'name': 'tactics', 'label': 'Тактика', 'icon': 'ti-chess'},
            {'name': 'effort', 'label': 'Самоотдача', 'icon': 'ti-flame'},
            {'name': 'organization', 'label': 'Организация', 'icon': 'ti-network'},
            {'name': 'mentality', 'label': 'Менталитет', 'icon': 'ti-brain'},
        ]
        context.update({
            'match': self.match, 'page_title': 'Шаг 2: Команды — DOPX',
            'step': 2, 'total_steps': 6, 'progress': session.progress_percentage(),
            'next_step': 'evaluations:players', 'prev_step': 'evaluations:context',
            'evaluation_fields': evaluation_fields,
        })
        return context


class EvaluatePlayersView(LoginRequiredMixin, TemplateView, EvaluationWizardMixin):
    template_name = 'evaluations/players.html'

    def dispatch(self, request, *args, **kwargs):
        redirect_response = self.prepare_step(request, kwargs['match_id'], 'teams')
        if redirect_response is not None:
            return redirect_response
        # Без состава шаг пройти нельзя.
        if not self.match.has_lineup:
            messages.warning(
                request,
                'Составы этого матча ещё не загружены. Попробуйте оценить игроков позже.',
            )
            return redirect('matches:detail', pk=self.match.id)
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        # Значения через форму — валидация 1..10.
        form = PlayerEvaluationForm(request.POST, match=self.match)
        if not form.is_valid():
            messages.error(request, 'Проверьте оценки игроков. Что-то введено некорректно.')
            return self.render_to_response(self.get_context_data(form=form))

        session = self.get_or_create_session()
        lineup_players = MatchLineupPlayer.objects.filter(lineup__match=self.match).select_related('player')
        lineup_total = lineup_players.count()
        count = 0
        with transaction.atomic():
            # Блокировка сессии от двойного POST.
            session = EvaluationSession.objects.select_for_update().get(pk=session.pk)
            is_new_step = 'players' not in session.completed_steps
            for lp in lineup_players:
                player = lp.player
                prefix = f'player_{player.id}'
                if form.cleaned_data.get(f'{prefix}_evaluate'):
                    contribution = form.cleaned_data.get(f'{prefix}_contribution')
                    risk = form.cleaned_data.get(f'{prefix}_risk')
                    potential = form.cleaned_data.get(f'{prefix}_potential')
                    # Включён тумблер, но ползунки не тронуты — оценку не сохраняем.
                    criteria = [f'{prefix}_contribution', f'{prefix}_risk', f'{prefix}_potential']
                    if not _touched_fields(request.POST, criteria):
                        continue
                    if contribution is not None and risk is not None and potential is not None:
                        PlayerEvaluation.objects.update_or_create(
                            user=request.user, match=self.match, player=player,
                            defaults={
                                'contribution': contribution,
                                'risk': risk,
                                'potential': potential,
                            }
                        )
                        count += 1
            self.update_session(session, 'players')
            if is_new_step and lineup_total:
                # XP пропорционален доле оценённых игроков состава.
                _award_step_xp(request, XP_PLAYERS_STEP_MAX * (count / lineup_total))
        messages.success(request, f'Оценено игроков: {count}.')
        return redirect('evaluations:coaches', match_id=self.match.id)

    def _stats_notable_player_ids(self, player_stats_by_id: dict) -> set:
        """Игроки, заметные по статистике (удары в створ, удары или сейвы выше порога)."""
        notable = set()
        for player_id, stats in player_stats_by_id.items():
            if (stats.shots_on_target or 0) >= STATS_KEY_PLAYER_SHOTS_ON_TARGET:
                notable.add(player_id)
            elif (stats.shots or 0) >= STATS_KEY_PLAYER_SHOTS:
                notable.add(player_id)
            elif (stats.saves or 0) >= STATS_KEY_PLAYER_SAVES:
                notable.add(player_id)
        return notable

    def _compute_key_player_ids(self, lineup_players: list, player_stats_by_id: dict) -> set:
        """Игроки, раскрытые по умолчанию в режиме «Быстро»:
        1. участники заметных событий или с заметной статистикой;
        2. добор стартовым составом по номеру до KEY_PLAYERS_PER_SIDE.
        """
        notable_ids = set(
            MatchEvent.objects.filter(
                match=self.match,
                event_type__in=('goal', 'yellow_card', 'red_card', 'own_goal', 'disallowed_goal'),
            ).values_list('player_id', flat=True)
        )
        notable_ids |= self._stats_notable_player_ids(player_stats_by_id)
        lineup_by_player = {lp.player_id: lp for lp in lineup_players}

        key_ids = {pid for pid in notable_ids if pid in lineup_by_player}
        for side in ('home', 'away'):
            side_count = sum(1 for pid in key_ids if lineup_by_player[pid].lineup.side == side)
            side_starters = sorted(
                (lp for lp in lineup_players if lp.lineup.side == side and lp.is_starting),
                key=lambda lp: lp.shirt_number if lp.shirt_number is not None else 99,
            )
            for lp in side_starters:
                if side_count >= KEY_PLAYERS_PER_SIDE:
                    break
                if lp.player_id not in key_ids:
                    key_ids.add(lp.player_id)
                    side_count += 1
        return key_ids

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        session = self.get_or_create_session()
        lineup_players = list(
            MatchLineupPlayer.objects.filter(lineup__match=self.match)
            .select_related('player__team')
            .order_by('is_starting', 'shirt_number')
        )

        # Статистика игроков за матч — для выбора ключевых и справки на карточке.
        player_stats_by_id = {
            stat.player_id: stat
            for stat in MatchPlayerStatistics.objects.filter(match=self.match)
        }
        for lp in lineup_players:
            stats = player_stats_by_id.get(lp.player_id)
            lp.match_stats = stats
            lp.stats_badges = _player_stats_badges(stats) if stats else []

        key_player_ids = (
            self._compute_key_player_ids(lineup_players, player_stats_by_id)
            if session.mode == 'quick' else set()
        )
        context.update({
            'match': self.match,
            'lineup_players': lineup_players,
            'mode': session.mode,
            'key_player_ids': key_player_ids,
            'home_bench_count': sum(1 for lp in lineup_players if not lp.is_starting and lp.lineup.side == 'home'),
            'away_bench_count': sum(1 for lp in lineup_players if not lp.is_starting and lp.lineup.side == 'away'),
            'page_title': 'Шаг 3: Игроки — DOPX', 'step': 3, 'total_steps': 6,
            'progress': session.progress_percentage(), 'next_step': 'evaluations:coaches', 'prev_step': 'evaluations:teams',
        })
        return context


class EvaluateCoachesView(LoginRequiredMixin, TemplateView, EvaluationWizardMixin):
    template_name = 'evaluations/coaches.html'

    def dispatch(self, request, *args, **kwargs):
        redirect_response = self.prepare_step(request, kwargs['match_id'], 'players')
        if redirect_response is not None:
            return redirect_response
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        # Значения через форму — валидация 1..10.
        form = CoachEvaluationForm(request.POST, match=self.match)
        if not form.is_valid():
            messages.error(request, 'Проверьте оценки тренеров. Что-то введено некорректно.')
            return self.render_to_response(self.get_context_data(form=form))

        session = self.get_or_create_session()
        with transaction.atomic():
            # Блокировка сессии от двойного POST.
            session = EvaluationSession.objects.select_for_update().get(pk=session.pk)
            is_new_step = 'coaches' not in session.completed_steps
            rated_coaches = 0
            total_coaches = 0
            for coach in [self.match.home_coach, self.match.away_coach]:
                if coach:
                    total_coaches += 1
                    prefix = f'coach_{coach.id}'
                    criteria = [
                        f'{prefix}_tactics', f'{prefix}_substitutions',
                        f'{prefix}_management', f'{prefix}_impact',
                    ]
                    # Ползунки не тронуты — не сохраняем.
                    if not _touched_fields(request.POST, criteria):
                        continue
                    CoachEvaluation.objects.update_or_create(
                        user=request.user, match=self.match, coach=coach,
                        defaults={
                            'tactics': form.cleaned_data[f'{prefix}_tactics'],
                            'substitutions': form.cleaned_data[f'{prefix}_substitutions'],
                            'game_management': form.cleaned_data[f'{prefix}_management'],
                            'impact': form.cleaned_data[f'{prefix}_impact'],
                        }
                    )
                    rated_coaches += 1
            self.update_session(session, 'coaches')
            if is_new_step:
                _award_step_xp(request, XP_COACHES_STEP)
        if total_coaches == 0:
            messages.info(request, 'Тренеры этого матча пока не загружены в базу.')
        elif rated_coaches:
            messages.success(request, f'Оценки тренеров сохранены: {rated_coaches} из {total_coaches}.')
        else:
            messages.info(request, 'Тренеры пропущены — ни один критерий не был отмечен.')
        return redirect('evaluations:referee', match_id=self.match.id)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        session = self.get_or_create_session()
        coaches = [c for c in [self.match.home_coach, self.match.away_coach] if c]
        context.update({
            'match': self.match, 'coaches': coaches, 'page_title': 'Шаг 4: Тренеры — DOPX',
            'step': 4, 'total_steps': 6, 'progress': session.progress_percentage(),
            'next_step': 'evaluations:referee', 'prev_step': 'evaluations:players',
            'mode': session.mode,
        })
        return context


class EvaluateRefereeView(LoginRequiredMixin, FormView, EvaluationWizardMixin):
    template_name = 'evaluations/referee.html'
    form_class = RefereeEvaluationForm

    def dispatch(self, request, *args, **kwargs):
        redirect_response = self.prepare_step(request, kwargs['match_id'], 'coaches')
        if redirect_response is not None:
            return redirect_response
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        session = self.get_or_create_session()
        # Ползунки не тронуты — не сохраняем.
        touched = _touched_fields(self.request.POST, ['influence_score', 'decision_quality'])
        with transaction.atomic():
            # Блокировка сессии от двойного POST.
            session = EvaluationSession.objects.select_for_update().get(pk=session.pk)
            is_new_step = 'referee' not in session.completed_steps
            if touched:
                RefereeEvaluation.objects.update_or_create(
                    user=self.request.user, match=self.match,
                    defaults={
                        'influence_score': form.cleaned_data.get('influence_score', 50),
                        'decision_quality': form.cleaned_data.get('decision_quality', 5)
                    }
                )
            self.update_session(session, 'referee')
            if is_new_step:
                _award_step_xp(self.request, XP_REFEREE_STEP)
        if touched:
            messages.success(self.request, 'Оценка судейства сохранена.')
        else:
            messages.info(self.request, 'Судейство пропущено — ни один критерий не был отмечен.')
        return redirect('evaluations:match_eval', match_id=self.match.id)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        session = self.get_or_create_session()
        context.update({
            'match': self.match, 'page_title': 'Шаг 5: Судья — DOPX',
            'step': 5, 'total_steps': 6, 'progress': session.progress_percentage(),
            'next_step': 'evaluations:match_eval', 'prev_step': 'evaluations:coaches',
            'mode': session.mode,
        })
        return context


class EvaluateMatchFinalView(LoginRequiredMixin, FormView, EvaluationWizardMixin):
    template_name = 'evaluations/match_final.html'
    form_class = MatchEvaluationForm

    def dispatch(self, request, *args, **kwargs):
        redirect_response = self.prepare_step(request, kwargs['match_id'], 'referee')
        if redirect_response is not None:
            return redirect_response
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        session = self.get_or_create_session()
        user = self.request.user

        with transaction.atomic():
            # Повторная проверка после лока — от двух одновременных запросов.
            session = EvaluationSession.objects.select_for_update().get(pk=session.pk)
            if session.status == 'completed':
                messages.info(self.request, 'Вы уже оценили этот матч')
                return redirect('matches:detail', pk=self.match.id)

            # 1. Финальная оценка матча
            MatchEvaluation.objects.update_or_create(
                user=user, match=self.match,
                defaults={
                    'entertainment': form.cleaned_data.get('entertainment', 5),
                    'tension': form.cleaned_data.get('tension', 5),
                    'turning_point': form.cleaned_data.get('turning_point', False),
                    'fairness': form.cleaned_data.get('fairness', 5),
                }
            )
            self.complete_session(session)

            # 2. Статистика пользователя (серия по турам)
            user.update_evaluation_stats(self.match)
            user.refresh_from_db()

            # 3. Trust Score — после закрытия голосования, по итоговому консенсусу.

            # 4. XP за финальный шаг. Достижения — асинхронно (п. 7).
            xp_result = _award_step_xp(self.request, XP_FINAL_STEP)
            xp = UserXP.objects.get(user=user)

            # Награда передаётся подписанным токеном в ссылке редиректа.
            # См. docs/adr/0015-evaluation-wizard-concurrency-and-reward-delivery.md.
            xp_gained = int(self.request.session.pop(_wizard_xp_key(self.request), 0))

            # 5. Уведомления о новом уровне.
            # При email_digest_mode письмо уходит в дайджест (email_sent_at=None).
            digest_mode = user.get_notification_setting('email_digest_mode', True)
            notification_sent_at = None if digest_mode else timezone.now()

            notifications_to_create = []
            if xp_result.get('level_increased'):
                for lvl in xp_result['levels_gained']:
                    notifications_to_create.append(Notification(
                        user=user,
                        notification_type='level_up',
                        title=f'⬆️ Новый уровень {lvl}!',
                        message=f'Поздравляем! Вы достигли уровня {lvl} с {xp.total_xp} XP.',
                        action_url='/users/profile/',
                        is_read=False,
                        related_match=self.match,
                        email_sent_at=notification_sent_at,
                    ))

            if notifications_to_create:
                Notification.objects.bulk_create(notifications_to_create)

        # Счётчик «ждут оценки» в навигации.
        from core.personal import forget_pending_count
        transaction.on_commit(partial(forget_pending_count, user.id))

        # 7. Достижения — асинхронно после коммита.
        transaction.on_commit(
            partial(check_and_award_badges_task.delay, user_id=str(user.id), match_id=str(self.match.id))
        )

        # 7.1. Аналитика — только после коммита.
        transaction.on_commit(
            partial(
                track_event, EventName.EVALUATION_COMPLETED,
                request=self.request, user=user,
                properties={"match_id": str(self.match.id)},
            )
        )

        # 7.2. Push о новом уровне — сразу, независимо от дайджеста.
        if xp_result.get('level_increased'):
            top_level = max(xp_result['levels_gained'])
            transaction.on_commit(
                partial(
                    send_push_task.delay, [str(user.id)],
                    f'⬆️ Новый уровень {top_level}!', f'У вас {xp.total_xp} XP — так держать.',
                    '/users/profile/', 'achievement', f'level-{user.id}',
                )
            )

        # 8. Мгновенные письма о новом уровне — только без дайджеста.
        if xp_result.get('level_increased') and not digest_mode:
            for lvl in xp_result['levels_gained']:
                transaction.on_commit(
                    partial(send_level_up_notification.delay, user_id=str(user.id), new_level=lvl, total_xp=xp.total_xp)
                )

        # 9. Антифрод: проверка скорости заполнения (асинхронно).
        transaction.on_commit(
            partial(flag_suspicious_wizard_speed_task.delay, session_id=str(session.id))
        )

        # 10. Пересчёт агрегатов
        try:
            transaction.on_commit(
                lambda: recalculate_all_aggregates_for_match.delay(str(self.match.id))
            )
        except Exception as e:
            logger.error(f"Ошибка постановки задачи агрегатов: {e}")

        messages.success(self.request, 'Оценка завершена. Спасибо за вклад.')

        # Сводка «во сколько оценок вы вошли» — считаем сразу по сохранённым строкам.
        rated_counts = {
            'teams': TeamEvaluation.objects.filter(user=user, match=self.match).count(),
            'players': PlayerEvaluation.objects.filter(user=user, match=self.match).count(),
            'coaches': CoachEvaluation.objects.filter(user=user, match=self.match).count(),
            'referee': RefereeEvaluation.objects.filter(user=user, match=self.match).count(),
        }
        # +1 — общая оценка матча.
        total_rated = 1 + sum(rated_counts.values())

        reward_token = signing.dumps(
            {'xp_gained': xp_gained, 'rated_counts': rated_counts, 'total_rated': total_rated},
            salt='evaluations.reward',
        )
        complete_url = reverse('evaluations:complete', args=[self.match.id])
        return redirect(f'{complete_url}?r={reward_token}')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        session = self.get_or_create_session()
        context.update({
            'match': self.match, 'page_title': 'Шаг 6: Финал — DOPX',
            'step': 6, 'total_steps': 6, 'progress': 100, 'prev_step': 'evaluations:referee',
            # Кнопки-пресеты на финальном шаге (docs/adr/0031-quick-mode-primary-flow.md).
            'mode': session.mode,
        })
        return context


class EvaluationCompleteView(LoginRequiredMixin, TemplateView):
    template_name = 'evaluations/complete.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        match_id = self.kwargs.get('match_id')
        if match_id:
            context['match'] = get_object_or_404(Match, id=match_id)

        # Награда из подписанного токена ?r= (max_age=300).
        reward = None
        token = self.request.GET.get('r')
        if token:
            try:
                reward = signing.loads(token, salt='evaluations.reward', max_age=300)
            except (signing.BadSignature, signing.SignatureExpired):
                reward = None
        context['xp_gained'] = reward['xp_gained'] if reward else None
        context['rated_counts'] = reward.get('rated_counts') if reward else None
        context['total_rated'] = reward.get('total_rated') if reward else None

        # Прогресс до следующего уровня.
        user_xp = getattr(self.request.user, 'xp', None)
        context['xp_progress_percent'] = user_xp.progress_percent if user_xp else 0
        context['user_level'] = user_xp.level if user_xp else 1

        context['page_title'] = 'Спасибо! — DOPX'
        return context