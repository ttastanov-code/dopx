# evaluations/views.py
"""
6-шаговый вайзард оценки матча. XP начисляется по шагам (контекст +2,
команды +2, игроки — до +3 пропорционально числу реально оценённых, тренеры
+1, судья +1, финал +1), домноженным на xp_multiplier() пользователя —
не фиксированным +10, чтобы наспамленные "5 всем" не давали тот же XP, что
внимательная оценка. check_and_award_badges уходит в check_and_award_badges_task
через transaction.on_commit (до ~100 запросов внутри HTTP-цикла иначе).
EvaluatePlayersView блокирует шаг, если match.has_lineup=False — иначе можно
"пройти" шаг с нулём оценённых игроков и получить полный XP.
EvaluateMatchFinalView пишет IP в EvaluationSession.ip_address и ставит
flag_suspicious_wizard_speed_task — асинхронный антифрод-сигнал по скорости заполнения.
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

from aggregates.services import calculate_user_trust_adjustment
from aggregates.tasks import recalculate_all_aggregates_for_match
from analytics.models import EventName
from analytics.services import track_event
from core.utils import get_client_ip
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
from matches.models import Match
from notifications.models import Notification
from notifications.tasks import send_level_up_notification
from users.models import UserXP
from users.tasks import check_and_award_badges_task, flag_suspicious_wizard_speed_task

import logging

logger = logging.getLogger(__name__)

# XP за каждый компонент вайзарда при полном прохождении. Сумма = 10,
# распределена по шагам и домножается на xp_multiplier().
XP_CONTEXT_STEP = 2
XP_TEAMS_STEP = 2
XP_PLAYERS_STEP_MAX = 3
XP_COACHES_STEP = 1
XP_REFEREE_STEP = 1
XP_FINAL_STEP = 1

# Режим "Быстро" (см. docs/adr/0006-quick-full-evaluation-mode.md) —
# сколько игроков на команду предзаполняется отмеченными на шаге "Игроки".
KEY_PLAYERS_PER_SIDE = 3


def _track_wizard_xp(request, amount: float) -> None:
    """
    Копит фактически начисленный XP за ТЕКУЩЕЕ прохождение вайзарда в сессии
    пользователя, шаг за шагом.

    Копится в сессии за все 6 шагов; EvaluateMatchFinalView.form_valid
    забирает и очищает — страница 'Спасибо' показывает реальную сумму,
    а не фиксированное число (зависит от xp_multiplier() и от того,
    скольких игроков реально оценили на шаге 3).
    """
    if amount <= 0:
        return
    request.session['wizard_xp_earned'] = request.session.get('wizard_xp_earned', 0) + amount


def _award_step_xp(request, base_amount: float) -> None:
    """
    Начисляет XP за прохождение одного шага вайзарда с учётом
    `user.xp_multiplier()`. Тихо не падает, если у пользователя почему-то
    ещё нет `UserXP` (на практике создаётся при регистрации в
    `users/views.py::RegisterView`, но защищаемся на случай рассинхронизации
    данных у существующих аккаунтов, заведённых до этого изменения).
    """
    if base_amount <= 0:
        return
    user = request.user
    xp, _created = UserXP.objects.get_or_create(user=user)
    gained = base_amount * user.xp_multiplier()
    xp.add_xp(gained)
    _track_wizard_xp(request, gained)


def _touched_fields(post_data, field_names: list) -> list:
    """
    Анти-шум критериев вайзарда (см. docs/adr/0005-anti-noise-touched-tracking.md
    и static/js/wizard-sliders.js). `<input type="range">` физически не может
    остаться пустым при отправке формы — JS на клиенте сопровождает каждый
    такой инпут скрытым полем "<name>__touched" ("1" после того, как
    пользователь реально подвинул ползунок, иначе "0"). Эта функция
    возвращает подмножество `field_names`, которые пользователь реально
    тронул.

    Деградация без JS: если ни для одного поля из field_names в POST вообще
    нет "__touched"-парного поля (JS не выполнился — отключён в браузере,
    очень старый User-Agent), функция считает ВСЕ поля тронутыми — иначе
    форма без JS выглядела бы так, будто пользователь ничего не оценил, хотя
    он честно подвинул все ползунки, просто без клиентской разметки этого
    факта. Деградирует до поведения ДО анти-шум фикса, не блокирует отправку.
    """
    js_ran = any(f'{name}__touched' in post_data for name in field_names)
    if not js_ran:
        return list(field_names)
    return [name for name in field_names if post_data.get(f'{name}__touched') == '1']


class EvaluationWizardMixin:
    def require_login_or_redirect(self, request):
        """
        Ранняя проверка авторизации для переопределённого dispatch() каждого
        шага вайзарда.

        LoginRequiredMixin сам по себе здесь не спасает: каждый шаг
        переопределяет dispatch() и обращается к self.get_or_create_session()
        (запрос EvaluationSession с user=request.user) ДО вызова
        super().dispatch() — то есть до того, как LoginRequiredMixin вообще
        успевает сработать. Для анонимного пользователя request.user —
        AnonymousUser, и ORM падает с ValidationError, пытаясь превратить его
        в UUID для фильтра user=... (реальный краш: 500 вместо чистого
        редиректа на логин).

        Возвращает HttpResponse-редирект на логин с ?next=, если пользователь
        не авторизован, иначе None — можно продолжать dispatch как обычно.
        Используем self.handle_no_permission() из AccessMixin (родитель
        LoginRequiredMixin) — тот же редирект, что дал бы штатный механизм,
        просто вызванный на шаг раньше, до обращения к БД.
        """
        if request.user.is_authenticated:
            return None
        messages.info(request, 'Войдите, чтобы оценить матч.')
        return self.handle_no_permission()

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
        now = timezone.now()
        if self.match.voting_open_until < now:
            return False, "Голосование для этого матча закрыто"
        if self.match.status != 'finished':
            return False, "Голосование доступно только для завершённых матчей"
        return True, None


class EvaluateContextView(LoginRequiredMixin, FormView, EvaluationWizardMixin):
    template_name = 'evaluations/context.html'
    form_class = ContextEvaluationForm

    def dispatch(self, request, *args, **kwargs):
        redirect_response = self.require_login_or_redirect(request)
        if redirect_response is not None:
            return redirect_response
        self.match = get_object_or_404(Match, id=kwargs['match_id'])
        can_vote, error_msg = self.check_voting_access()
        if not can_vote:
            messages.error(request, error_msg)
            return redirect('matches:detail', pk=self.match.id)
        if EvaluationSession.objects.filter(user=request.user, match=self.match, status='completed').exists():
            messages.info(request, 'Вы уже оценили этот матч.')
            return redirect('matches:detail', pk=self.match.id)
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['match'] = self.match
        return kwargs

    def form_valid(self, form):
        with transaction.atomic():
            # select_for_update() против гонки двойного сабмита (двойной
            # клик/два таба). См.
            # docs/adr/0015-evaluation-wizard-concurrency-and-reward-delivery.md.
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
            # Режим "Быстро/Подробно" (см. docs/adr/0006-quick-full-evaluation-mode.md)
            # — выбирается один раз, здесь, на первом шаге. Не часть
            # ContextEvaluationForm/ContextEvaluation: это метаданные ПРОХОЖДЕНИЯ
            # вайзарда (что показывать на следующих шагах), а не мнение
            # пользователя о матче. Значение по умолчанию 'full' — если
            # переключатель почему-то не пришёл в POST (JS выключен, старая
            # закладка без него), поведение не меняется относительно того,
            # что было до этой фичи.
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
        redirect_response = self.require_login_or_redirect(request)
        if redirect_response is not None:
            return redirect_response
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
        # БАГ, КОТОРЫЙ ТУТ БЫЛ: поля тактика/самоотдача/организация/
        # менталитет читались напрямую через int(request.POST.get(...)),
        # минуя TeamEvaluationForm (которая уже описана в evaluations/
        # forms.py с MinValueValidator(1)/MaxValueValidator(10)) — сырой
        # POST с произвольным числом (или вообще без числа) либо падал
        # ValueError-ом наружу, либо тихо проходил валидацию 1..10 никак не
        # проверенной. Теперь значения идут через form.cleaned_data.
        form = TeamEvaluationForm(request.POST, match=self.match)
        if not form.is_valid():
            messages.error(request, 'Проверьте оценки команд. Что-то введено некорректно.')
            return self.render_to_response(self.get_context_data(form=form))

        session = self.get_or_create_session()
        with transaction.atomic():
            # БАГ, КОТОРЫЙ ТУТ БЫЛ: см. EvaluateContextView.form_valid —
            # та же гонка двойного POST без блокировки строки session.
            session = EvaluationSession.objects.select_for_update().get(pk=session.pk)
            is_new_step = 'teams' not in session.completed_steps
            rated_teams = 0
            for team in [self.match.home_team, self.match.away_team]:
                prefix = f'team_{team.id}'
                criteria = [f'{prefix}_tactics', f'{prefix}_effort', f'{prefix}_organization', f'{prefix}_mentality']
                # Анти-шум (см. _touched_fields): ни один ползунок для этой
                # команды не тронут — не создаём TeamEvaluation с "5,5,5,5"
                # по умолчанию, это не оценка, а тишина.
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
        redirect_response = self.require_login_or_redirect(request)
        if redirect_response is not None:
            return redirect_response
        self.match = get_object_or_404(Match, id=kwargs['match_id'])
        can_vote, error_msg = self.check_voting_access()
        if not can_vote:
            messages.error(request, error_msg)
            return redirect('matches:detail', pk=self.match.id)
        session = self.get_or_create_session()
        if 'teams' not in session.completed_steps:
            return redirect('evaluations:teams', match_id=self.match.id)
        # Без состава (парсер ещё не загрузил lineup) шаг не даёт пройти —
        # иначе можно "пройти" его с нулём оценённых игроков и получить
        # полный XP наравне с тем, кто оценил 15+.
        if not self.match.has_lineup:
            messages.warning(
                request,
                'Составы этого матча ещё не загружены. Попробуйте оценить игроков позже.',
            )
            return redirect('matches:detail', pk=self.match.id)
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        # БАГ, КОТОРЫЙ ТУТ БЫЛ (найден повторным ручным разбором после
        # AUDIT_2026-08.md — при первом проходе фикса этот шаг ошибочно
        # сочли уже безопасным из-за try/except ниже): try/except (ValueError,
        # TypeError) ловит только "не число", но НЕ диапазон — PlayerEvaluationForm
        # уже описана в evaluations/forms.py с MinValueValidator(1)/
        # MaxValueValidator(10), но не использовалась здесь. POST с
        # player_X_contribution=999999 (или отрицательным) проходил
        # int(...) без единой проверки границ и ломал performance_score
        # игрока, особенно при малой выборке голосов. Теперь — через форму,
        # как Teams/Coaches.
        form = PlayerEvaluationForm(request.POST, match=self.match)
        if not form.is_valid():
            messages.error(request, 'Проверьте оценки игроков. Что-то введено некорректно.')
            return self.render_to_response(self.get_context_data(form=form))

        session = self.get_or_create_session()
        lineup_players = MatchLineupPlayer.objects.filter(lineup__match=self.match).select_related('player')
        lineup_total = lineup_players.count()
        count = 0
        with transaction.atomic():
            # БАГ, КОТОРЫЙ ТУТ БЫЛ: см. EvaluateContextView.form_valid —
            # та же гонка двойного POST без блокировки строки session.
            session = EvaluationSession.objects.select_for_update().get(pk=session.pk)
            is_new_step = 'players' not in session.completed_steps
            for lp in lineup_players:
                player = lp.player
                prefix = f'player_{player.id}'
                if form.cleaned_data.get(f'{prefix}_evaluate'):
                    contribution = form.cleaned_data.get(f'{prefix}_contribution')
                    risk = form.cleaned_data.get(f'{prefix}_risk')
                    potential = form.cleaned_data.get(f'{prefix}_potential')
                    # Анти-шум (см. _touched_fields): включить тумблер
                    # "оценить" — это ещё не то же самое, что подвинуть хотя
                    # бы один из трёх ползунков. Тумблер сам по себе уже
                    # неплохой сигнал намерения, но без этой проверки
                    # "включил и сразу дальше" тихо сохранял бы "5,5,5" как
                    # реальную оценку контрибуции/риска/потенциала.
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
                # XP пропорционален доле реально оценённых игроков от
                # состава — не фиксированная сумма за формальное
                # "прохождение" шага.
                _award_step_xp(request, XP_PLAYERS_STEP_MAX * (count / lineup_total))
        messages.success(request, f'Оценено игроков: {count}.')
        return redirect('evaluations:coaches', match_id=self.match.id)

    def _compute_key_player_ids(self, lineup_players: list) -> set:
        """
        Курируемый набор игроков для режима "Быстро" — предзаполненные
        карточки, чтобы не листать весь состав ради 3-5 самых заметных
        участников матча. См. docs/adr/0006-quick-full-evaluation-mode.md.

        Приоритет 1 — участники заметных событий матча (гол/красная/жёлтая
        карточка/автогол — самый дешёвый доступный прокси "заметности" без
        отдельной метрики минут на поле). Приоритет 2 — добивание до
        KEY_PLAYERS_PER_SIDE игроков на команду стартовым составом по
        возрастанию номера (детерминированно, не полагается на предположение
        "капитан = игрок с наименьшим номером", которого в модели нет).
        """
        notable_ids = set(
            MatchEvent.objects.filter(
                match=self.match,
                event_type__in=('goal', 'yellow_card', 'red_card', 'own_goal', 'disallowed_goal'),
            ).values_list('player_id', flat=True)
        )
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
        key_player_ids = self._compute_key_player_ids(lineup_players) if session.mode == 'quick' else set()
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
        redirect_response = self.require_login_or_redirect(request)
        if redirect_response is not None:
            return redirect_response
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
        # БАГ, КОТОРЫЙ ТУТ БЫЛ: см. EvaluateTeamsView.post — поля тактика/
        # замены/управление/влияние читались напрямую через
        # int(request.POST.get(...)), минуя CoachEvaluationForm (уже
        # описана в evaluations/forms.py с валидацией 1..10).
        form = CoachEvaluationForm(request.POST, match=self.match)
        if not form.is_valid():
            messages.error(request, 'Проверьте оценки тренеров. Что-то введено некорректно.')
            return self.render_to_response(self.get_context_data(form=form))

        session = self.get_or_create_session()
        with transaction.atomic():
            # БАГ, КОТОРЫЙ ТУТ БЫЛ: см. EvaluateContextView.form_valid —
            # та же гонка двойного POST без блокировки строки session.
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
                    # Анти-шум — см. _touched_fields и EvaluateTeamsView.post.
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
        redirect_response = self.require_login_or_redirect(request)
        if redirect_response is not None:
            return redirect_response
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
        # Анти-шум — см. _touched_fields и EvaluateTeamsView.post. Ни один
        # из двух ползунков не тронут — не создаём RefereeEvaluation с
        # дефолтами "50, 5", это не оценка, а молчание.
        touched = _touched_fields(self.request.POST, ['influence_score', 'decision_quality'])
        with transaction.atomic():
            # БАГ, КОТОРЫЙ ТУТ БЫЛ: см. EvaluateContextView.form_valid —
            # та же гонка двойного POST без блокировки строки session.
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
        redirect_response = self.require_login_or_redirect(request)
        if redirect_response is not None:
            return redirect_response
        self.match = get_object_or_404(Match, id=kwargs['match_id'])
        can_vote, error_msg = self.check_voting_access()
        if not can_vote:
            messages.error(request, error_msg)
            return redirect('matches:detail', pk=self.match.id)
        session = self.get_or_create_session()
        if 'referee' not in session.completed_steps:
            return redirect('evaluations:referee', match_id=self.match.id)
        # БАГ, КОТОРЫЙ ТУТ БЫЛ: проверялось только 'referee' in completed_steps
        # — этого достаточно, чтобы пройти на страницу, но НЕ защищает от
        # повторного POST на уже завершённую сессию (status == 'completed'):
        # form_valid ниже заново начисляет XP, заново корректирует Trust
        # Score и заново ставит анти-фрод/аналитику задачи. Тот же паттерн,
        # что уже используется в EvaluateContextView.dispatch для входа в
        # вайзард заново.
        if session.status == 'completed':
            messages.info(request, 'Вы уже оценили этот матч.')
            return redirect('matches:detail', pk=self.match.id)
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        session = self.get_or_create_session()
        user = self.request.user

        with transaction.atomic():
            # Проверка session.status=='completed' в dispatch() не ловит два
            # ОДНОВРЕМЕННЫХ запроса — повторная проверка после лока. См.
            # docs/adr/0015-evaluation-wizard-concurrency-and-reward-delivery.md.
            session = EvaluationSession.objects.select_for_update().get(pk=session.pk)
            if session.status == 'completed':
                messages.info(self.request, 'Вы уже оценили этот матч')
                return redirect('matches:detail', pk=self.match.id)

            old_trust = user.trust_score

            # 1. Сохраняем финальную оценку матча
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

            # 2. Обновляем статистику пользователя (серия — по турам, см.
            #    докстринг User.evaluation_streak/update_evaluation_stats)
            user.update_evaluation_stats(self.match)
            user.refresh_from_db()

            # 3. Корректируем Trust Score
            adjustment = calculate_user_trust_adjustment(user, self.match)
            new_trust = max(0.5, min(2.0, user.trust_score + adjustment))
            if abs(new_trust - user.trust_score) >= 0.01:
                user.trust_score = new_trust
                user.save(update_fields=['trust_score', 'updated_at'])

            # 4. Начисляем XP за финальный шаг (компонентная схема — см.
            #    докстринг модуля, пункт 1). Проверка и выдача достижений
            #    ПЕРЕЕХАЛА в асинхронную задачу (пункт 2) — не считаем и не
            #    уведомляем о бейджах здесь синхронно.
            xp, _ = UserXP.objects.get_or_create(user=user)
            final_step_gained = XP_FINAL_STEP * user.xp_multiplier()
            xp_result = xp.add_xp(final_step_gained)
            _track_wizard_xp(self.request, final_step_gained)

            # Награда передаётся подписанным токеном в query-string
            # редиректа, не через session.pop() (одноразовый, "съедался" до
            # рендера страницы у части пользователей). См.
            # docs/adr/0015-evaluation-wizard-concurrency-and-reward-delivery.md.
            xp_gained = round(self.request.session.pop('wizard_xp_earned', 0), 1)
            trust_delta = round(new_trust - old_trust, 3)

            # 5. Уведомления о повышении уровня (поддержка скачков через
            #    несколько уровней сразу) и об изменении Trust Score.
            #    ДАЙДЖЕСТ: если у пользователя включён `email_digest_mode`
            #    (по умолчанию — да), мгновенное письмо НЕ ставится в
            #    очередь ниже — только in-app уведомление с `email_sent_at
            #    =None`, которое подхватит `notifications/tasks.py::
            #    send_notification_digest`. Иначе письмо уходит мгновенно, и
            #    `email_sent_at` проставляется сразу, чтобы дайджест не
            #    отправил его повторно.
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

            # 6. Уведомление об изменении Trust Score
            if abs(user.trust_score - old_trust) >= 0.1:
                notifications_to_create.append(Notification(
                    user=user,
                    notification_type='system',
                    title='🛡️ Ваш Trust Score обновлён',
                    message=f'Ваш уровень доверия: {round(old_trust, 2)} → {round(user.trust_score, 2)}',
                    action_url='/users/profile/',
                    is_read=False,
                    related_match=self.match,
                    email_sent_at=notification_sent_at,
                ))

            if notifications_to_create:
                Notification.objects.bulk_create(notifications_to_create)

        # 7. Достижения — асинхронно, ПОСЛЕ коммита транзакции (см. пункт 2
        #    докстринга модуля). Уведомления о новых бейджах создаются и
        #    рассылаются внутри самой задачи (users/tasks.py), а не здесь.
        transaction.on_commit(
            partial(check_and_award_badges_task.delay, user_id=str(user.id), match_id=str(self.match.id))
        )

        # 7.1. Продуктовая аналитика — событие "оценка матча завершена".
        #    on_commit: если транзакция откатится (редкий случай гонки с
        #    IntegrityError и т.п.), событие не должно быть записано —
        #    иначе воронка регистрирует оценки, которых на самом деле нет
        #    в базе.
        transaction.on_commit(
            partial(
                track_event, EventName.EVALUATION_COMPLETED,
                request=self.request, user=user,
                properties={"match_id": str(self.match.id)},
            )
        )

        # 8. Мгновенные письма о повышении уровня — ТОЛЬКО если у пользователя
        #    выключен дайджест (см. пункт 5 выше); иначе уведомление уже
        #    создано с email_sent_at=None и будет отправлено одним письмом
        #    вместе с остальными через send_notification_digest.
        if xp_result.get('level_increased') and not digest_mode:
            for lvl in xp_result['levels_gained']:
                transaction.on_commit(
                    partial(send_level_up_notification.delay, user_id=str(user.id), new_level=lvl, total_xp=xp.total_xp)
                )

        # 9. Анти-фрод: асинхронная проверка скорости заполнения вайзарда
        #    (см. докстринг модуля, пункт 4). Не блокирует ответ пользователю
        #    и не отменяет уже сохранённые оценки — только помечает сессию
        #    для ручной модерации при подозрительно быстром заполнении.
        transaction.on_commit(
            partial(flag_suspicious_wizard_speed_task.delay, session_id=str(session.id))
        )

        # 10. Запускаем пересчёт агрегатов
        try:
            transaction.on_commit(
                lambda: recalculate_all_aggregates_for_match.delay(str(self.match.id))
            )
        except Exception as e:
            logger.error(f"Ошибка постановки задачи агрегатов: {e}")

        messages.success(self.request, 'Оценка завершена. Спасибо за вклад.')

        # Итоговая сводка "во сколько оценок вы вошли" (запрос из
        # docs/adr/0006-quick-full-evaluation-mode.md, п. "мгновенная
        # отдача после отправки") — считаем СРАЗУ здесь, синхронно, из уже
        # закоммиченных строк текущего пользователя, а не ждём асинхронный
        # пересчёт агрегатов (п. 10 ниже уходит в Celery и к моменту
        # рендера страницы "Спасибо" мог ещё не отработать). Сознательно
        # НЕ показываем "поднялся на N позиций в рейтинге" — это требует
        # сравнения агрегата ДО/ПОСЛЕ пересчёта, честно посчитать которое
        # можно только после того, как Celery-задача выше реально
        # отработает; строить это на догадках значило бы городить ещё один
        # источник недоверия к рейтингу, а не бороться с ним. См.
        # "Последствия" в ADR — отдельная задача на будущее.
        rated_counts = {
            'teams': TeamEvaluation.objects.filter(user=user, match=self.match).count(),
            'players': PlayerEvaluation.objects.filter(user=user, match=self.match).count(),
            'coaches': CoachEvaluation.objects.filter(user=user, match=self.match).count(),
            'referee': RefereeEvaluation.objects.filter(user=user, match=self.match).count(),
        }
        # +1 — сама общая оценка матча (MatchEvaluation), которая создана
        # безусловно чуть выше в этой же транзакции.
        total_rated = 1 + sum(rated_counts.values())

        # Награду передаём подписанным токеном в query-string, а не через
        # session — см. комментарий у объявления xp_gained/trust_delta выше.
        reward_token = signing.dumps(
            {
                'xp_gained': xp_gained, 'trust_delta': trust_delta,
                'rated_counts': rated_counts, 'total_rated': total_rated,
            },
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
        })
        return context


class EvaluationCompleteView(LoginRequiredMixin, TemplateView):
    template_name = 'evaluations/complete.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        match_id = self.kwargs.get('match_id')
        if match_id:
            context['match'] = get_object_or_404(Match, id=match_id)

        # Подписанный токен в ?r= вместо одноразового session.pop() — см.
        # комментарий в EvaluateMatchFinalView.form_valid(). Токен можно
        # читать сколько угодно раз (обновление страницы больше не съедает
        # цифры), но max_age=300 не даёт пользователю держать вкладку открытой
        # неделями и показывать чужие/стухшие значения, а подпись не даёт
        # подделать цифры руками через адресную строку.
        reward = None
        token = self.request.GET.get('r')
        if token:
            try:
                reward = signing.loads(token, salt='evaluations.reward', max_age=300)
            except (signing.BadSignature, signing.SignatureExpired):
                reward = None
        context['xp_gained'] = reward['xp_gained'] if reward else None
        context['trust_delta'] = reward['trust_delta'] if reward else None
        # .get() — не reward['...']: токен, подписанный ДО деплоя этой правки
        # (max_age=300с — теоретически ещё живой в первые секунды после
        # раскатки) не будет содержать этих двух ключей.
        context['rated_counts'] = reward.get('rated_counts') if reward else None
        context['total_rated'] = reward.get('total_rated') if reward else None

        # Реальный прогресс до следующего уровня (UserXP.progress_percent
        # уже правильно считает от границ текущего/следующего уровня — см.
        # users/models.py) вместо бессмысленного total_evaluations+10.
        user_xp = getattr(self.request.user, 'xp', None)
        context['xp_progress_percent'] = user_xp.progress_percent if user_xp else 0
        context['user_level'] = user_xp.level if user_xp else 1

        context['page_title'] = 'Спасибо! — DOPX'
        return context