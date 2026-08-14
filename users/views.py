# users/views.py
"""
ИЗМЕНЕНИЯ (продуктовый аудит DOPX, часть 2):

1. `RegisterView` — раньше регистрация была полностью открыта для
   автоматического массового создания аккаунтов: ни rate-limit, ни IP/UA
   логирования. Добавлено: rate-limit по IP через `core.utils.
   is_rate_limited` (не более `REGISTER_RATE_LIMIT` регистраций с одного IP
   за `REGISTER_RATE_LIMIT_WINDOW_SECONDS`), сохранение `registration_ip`/
   `registration_user_agent` на созданном пользователе (антифрод-данные для
   будущего кластерного анализа — см. продуктовый аудит, раздел 4.3).
   Honeypot/time-trap проверяются самой формой (`users/forms.py`) —
   `form_invalid` сработает автоматически через стандартный Django-флоу,
   отдельного кода здесь не требует.
2. `VerifyEmailView` — после успешной верификации ставится в очередь
   `users.tasks.award_founder_badge_if_eligible` (бейдж «Первопроходец»,
   разовая проверка, см. её докстринг).
3. `NotificationSettingsView.form_valid` — раньше вручную перечислял 5
   конкретных ключей настроек, из-за чего любое новое поле в форме (как
   `email_digest_mode`, добавленный сейчас для дайджест-рассылки) молча
   игнорировалось бы при сохранении. Переписано на генерацию словаря из
   `User.DEFAULT_NOTIFICATION_SETTINGS.keys()` — новые настройки подхватываются
   автоматически, если добавлено соответствующее поле в форме.
"""
from __future__ import annotations

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import login, logout, authenticate, update_session_auth_hash
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.views import (
    LoginView as AuthLoginView,
    PasswordResetView, PasswordResetDoneView,
    PasswordResetConfirmView, PasswordResetCompleteView
)
from django.contrib import messages
from django.views.generic import CreateView, TemplateView, ListView, UpdateView, FormView, View
from django.urls import reverse_lazy
from django.db.models import Count, Avg, Q
from django.utils import timezone
from django.conf import settings
from datetime import timedelta

from core.utils import get_client_ip, is_rate_limited
from users.models import User, UserBadge, UserXP
from users.forms import (
    UserRegistrationForm, UserLoginForm, UserProfileForm,
    CustomPasswordChangeForm, CustomPasswordResetForm, NotificationSettingsForm
)
from notifications.models import Notification
import logging

logger = logging.getLogger(__name__)

REGISTER_RATE_LIMIT = 5
REGISTER_RATE_LIMIT_WINDOW_SECONDS = 60 * 60  # 1 час


class RegisterView(CreateView):
    """Регистрация нового пользователя с обязательной верификацией почты"""
    model = User
    form_class = UserRegistrationForm
    template_name = 'auth/register.html'
    success_url = reverse_lazy('users:verify_email_sent')

    def dispatch(self, request, *args, **kwargs):
        # Rate-limit ДО обработки формы — не тратим время на валидацию
        # (и не даём боту вообще понять, что его лимитировали по форме).
        client_ip = get_client_ip(request)
        if request.method == 'POST' and client_ip:
            if is_rate_limited(f'register:{client_ip}', REGISTER_RATE_LIMIT, REGISTER_RATE_LIMIT_WINDOW_SECONDS):
                logger.warning(f"⚠️ Registration rate limit exceeded for IP {client_ip}")
                messages.error(request, '⚠️ Слишком много попыток регистрации. Попробуйте позже.')
                return redirect('users:register')
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        user = form.save(commit=False)
        user.is_verified = False  # Аккаунт создан, но не активирован
        user.set_password(form.cleaned_data['password1'])
        # НОВОЕ: антифрод-данные регистрации (см. докстринг модуля, пункт 1).
        user.registration_ip = get_client_ip(self.request)
        user.registration_user_agent = self.request.META.get('HTTP_USER_AGENT', '')[:1000]
        user.save()
        # Создаем базовые профили
        UserXP.objects.get_or_create(user=user)

        # Отправляем письмо верификации (асинхронно, КРИТИЧЕСКОЕ - force=True)
        try:
            from notifications.tasks import send_email_verification
            send_email_verification.delay(str(user.id), str(user.verification_token))
            logger.info(f"Verification email queued for {user.email}")
        except Exception as e:
            logger.error(f"Failed to queue verification email: {e}")
            # Фоллбэк: синхронно, чтобы не потерять пользователя
            try:
                from notifications.tasks import _send_email_to_user
                site_url = getattr(settings, 'SITE_URL', 'http://127.0.0.1:8000')
                verify_url = f"{site_url}/users/verify-email/{user.verification_token}/"
                _send_email_to_user(user, '👋 Подтвердите email на DOPX', 'emails/verify_email.html', {'verify_url': verify_url}, force=True)
            except Exception as fallback_e:
                logger.critical(f"CRITICAL: Failed to send verification email synchronously: {fallback_e}")

        messages.success(self.request, '✅ Регистрация прошла успешно! Проверьте почту для активации.')
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = 'Регистрация — DOPX'
        return context


class VerifyEmailSentView(TemplateView):
    template_name = 'auth/verify_email_sent.html'
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = 'Подтвердите email — DOPX'
        return context


class VerifyEmailView(View):
    """Обработка клика по ссылке из письма для верификации"""
    def get(self, request, token):
        try:
            user = User.objects.get(verification_token=token, is_verified=False)
            # Проверка срока действия токена (48 часов)
            token_age = timezone.now() - user.verification_token_created_at
            if token_age > timedelta(hours=48):
                messages.error(request, '❌ Ссылка для верификации устарела. Зарегистрируйтесь заново.')
                return redirect('users:register')

            # Активируем аккаунт
            user.is_verified = True
            user.save(update_fields=['is_verified', 'updated_at'])

            # Автоматический вход
            login(request, user)

            # Приветственное уведомление (критическое, не отключается)
            Notification.objects.create(
                user=user,
                notification_type='welcome',
                title='👋 Добро пожаловать в DOPX!',
                message='Ваш аккаунт активирован. Оценивайте матчи и получайте достижения!',
                action_url='/matches/',
                is_read=False,
            )

            # НОВОЕ: разовая проверка бейджа «Первопроходец» (users/tasks.py) —
            # асинхронно, не блокирует ответ пользователю.
            try:
                from users.tasks import award_founder_badge_if_eligible
                award_founder_badge_if_eligible.delay(str(user.id))
            except Exception as e:
                logger.error(f"Failed to queue founder badge check: {e}")

            messages.success(request, '🎉 Почта подтверждена! Добро пожаловать.')
            return redirect('core:home')
        except User.DoesNotExist:
            messages.error(request, '❌ Неверная или уже использованная ссылка.')
            return redirect('users:verify_email_invalid')


class VerifyEmailInvalidView(TemplateView):
    template_name = 'auth/verify_email_invalid.html'
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = 'Ошибка верификации — DOPX'
        return context


class LoginView(AuthLoginView):
    """Вход с проверкой верификации почты"""
    template_name = 'auth/login.html'
    authentication_form = UserLoginForm

    def form_valid(self, form):
        user = form.get_user()
        # БЛОКИРОВКА: Если почта не подтверждена — не пускаем
        if not user.is_verified:
            try:
                from notifications.tasks import send_email_verification
                send_email_verification.delay(str(user.id), str(user.verification_token))
            except Exception:
                pass
            messages.warning(self.request, '⚠️ Ваша почта не подтверждена. Мы отправили новое письмо.')
            return redirect('users:login')

        response = super().form_valid(form)
        remember_me = self.request.POST.get('remember')
        if not remember_me:
            self.request.session.set_expiry(0)
        else:
            self.request.session.set_expiry(1209600)
        messages.success(self.request, f'👋 С возвращением, {user.username}!')
        return response

    def get_success_url(self):
        next_url = self.request.GET.get('next')
        return next_url if next_url else reverse_lazy('core:home')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = 'Вход — DOPX'
        return context


class LogoutView(LoginRequiredMixin, View):
    def get(self, request, *args, **kwargs):
        logout(request)
        messages.info(request, '👋 Вы вышли из аккаунта')
        return redirect('core:home')


class ProfileView(LoginRequiredMixin, TemplateView):
    template_name = 'profile/dashboard.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        stats = {
            'total_evaluations': user.total_evaluations,
            'total_players': user.player_evaluations.values('player').distinct().count(),
            'trust_score': round(user.trust_score, 2),
            'trust_level': user.get_trust_level(),
            'evaluation_streak': user.evaluation_streak,
            'total_matches': user.evaluation_sessions.filter(status='completed').count(),
        }
        recent_evaluations = user.context_evaluations.select_related(
            'match__home_team', 'match__away_team'
        ).order_by('-created_at')[:10]
        # rarity/is_secret — properties поверх users/badges.py::BADGE_CATALOG,
        # доступны в шаблоне как badge.rarity / badge.is_secret / badge.description.
        badges = UserBadge.objects.filter(user=user).order_by('-awarded_at')
        xp, _ = UserXP.objects.get_or_create(user=user)
        active_sessions = user.evaluation_sessions.filter(
            status__in=['started', 'in_progress']
        ).select_related('match').order_by('-created_at')[:5]

        context.update({
            'user': user,
            'stats': stats,
            'recent_evaluations': recent_evaluations,
            'badges': badges,
            'xp': xp,
            'active_sessions': active_sessions,
            'page_title': f'Профиль — {user.username}'
        })
        return context


class ProfileEditView(LoginRequiredMixin, UpdateView):
    model = User
    form_class = UserProfileForm
    template_name = 'users/profile_edit.html'
    success_url = reverse_lazy('users:profile')

    def get_object(self):
        return self.request.user

    def form_valid(self, form):
        if form.cleaned_data.get('delete_avatar') and self.object.avatar:
            self.object.avatar.delete(save=False)
            self.object.avatar = None
        messages.success(self.request, '✅ Профиль обновлён')
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = 'Редактирование профиля — DOPX'
        return context


class PasswordChangeViewCustom(LoginRequiredMixin, FormView):
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
        messages.success(self.request, '✅ Пароль изменён')
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = 'Изменить пароль — DOPX'
        return context


class PasswordResetViewCustom(PasswordResetView):
    template_name = 'auth/password_reset.html'
    email_template_name = 'emails/password_reset_email.txt'
    html_email_template_name = 'emails/password_reset_email.html'
    subject_template_name = 'emails/password_reset_subject.txt'
    success_url = reverse_lazy('users:password_reset_done')
    form_class = CustomPasswordResetForm

    def form_valid(self, form):
        messages.success(self.request, '✅ Инструкция отправлена')
        return super().form_valid(form)


class PasswordResetDoneViewCustom(PasswordResetDoneView):
    template_name = 'auth/password_reset_done.html'


class PasswordResetConfirmViewCustom(PasswordResetConfirmView):
    template_name = 'auth/password_reset_confirm.html'
    success_url = reverse_lazy('users:password_reset_complete')


class PasswordResetCompleteViewCustom(PasswordResetCompleteView):
    template_name = 'auth/password_reset_complete.html'


class NotificationSettingsView(LoginRequiredMixin, FormView):
    template_name = 'users/notification_settings.html'
    form_class = NotificationSettingsForm
    success_url = reverse_lazy('users:profile')

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['user'] = self.request.user
        return kwargs

    def form_valid(self, form):
        user = self.request.user
        # ИСПРАВЛЕНО: раньше здесь были захардкожены 5 конкретных ключей —
        # новое поле формы (например, `email_digest_mode`) молча
        # игнорировалось бы при сохранении, пока кто-то не вспомнил бы
        # добавить его в этот список вручную. Теперь ключи берутся из
        # единого источника — `User.DEFAULT_NOTIFICATION_SETTINGS`.
        user._notification_settings = {
            key: form.cleaned_data.get(key, True)
            for key in User.DEFAULT_NOTIFICATION_SETTINGS
        }
        user.save(update_fields=['_notification_settings', 'updated_at'])
        user.refresh_from_db()  # Сбрасываем кэш свойств
        messages.success(self.request, '✅ Настройки уведомлений сохранены')
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = 'Настройки уведомлений — DOPX'
        return context


class UserLeaderboardView(ListView):
    model = User
    template_name = 'users/leaderboard.html'
    context_object_name = 'users'
    paginate_by = 20

    def get_queryset(self):
        return User.objects.filter(is_active=True, is_verified=True).annotate(
            eval_count=Count('context_evaluations', distinct=True)
        ).filter(eval_count__gte=1).order_by('-trust_score', '-eval_count')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = 'Рейтинг пользователей — DOPX'
        return context


class PlayerLeaderboardView(ListView):
    template_name = 'players/leaderboard.html'
    context_object_name = 'players'
    paginate_by = 20

    def get_queryset(self):
        from players.models import Player
        from aggregates.models import PlayerMatchAggregate
        from django.db.models import Avg, Count, Sum, Q
        return Player.objects.filter(is_active=True).annotate(
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
        return context