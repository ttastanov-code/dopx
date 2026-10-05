# users/views.py
"""Регистрация, вход, профиль, лидерборды, подписки и push.

При регистрации сохраняем IP и user-agent (антифрод), лимит регистраций с IP.
После верификации email проверяется бейдж «Первопроходец».
"""
from __future__ import annotations

from django.http import Http404, JsonResponse, HttpResponse
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
from django.urls import reverse, reverse_lazy
from django.utils.http import url_has_allowed_host_and_scheme
from django.db.models import Count, Avg, Q, F, Sum, Window

from evaluations.completed import completed_only
from aggregates.services import vote_weighted_avg
from django.db.models.functions import RowNumber
from django.utils import timezone
from django.conf import settings
from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST

from analytics.models import EventName
from analytics.services import track_event
from core.utils import get_auth_panel_stats, get_client_ip, is_rate_limited, service_accounts_q
from core.models import get_setting
from users.badges import BADGE_CATALOG, RARITY_ORDER
from users.models import Follow, User, UserBadge, UserXP
from users.forms import (
    UserRegistrationForm, UserLoginForm, UserProfileForm,
    CustomPasswordChangeForm, CustomPasswordResetForm, NotificationSettingsForm
)
from notifications.models import Notification
import logging

logger = logging.getLogger(__name__)

REGISTER_RATE_LIMIT = 5
REGISTER_RATE_LIMIT_WINDOW_SECONDS = 60 * 60  # 1 час

# Лимиты: анонимные эндпоинты — по IP, эндпоинты за логином — по user.id.
PASSWORD_RESET_RATE_LIMIT = 5
PASSWORD_RESET_RATE_LIMIT_WINDOW_SECONDS = 60 * 60  # 1 час
VERIFY_EMAIL_RATE_LIMIT = 20
VERIFY_EMAIL_RATE_LIMIT_WINDOW_SECONDS = 60 * 10  # 10 минут
RESEND_VERIFICATION_RATE_LIMIT = 3
RESEND_VERIFICATION_RATE_LIMIT_WINDOW_SECONDS = 60 * 60  # 1 час
FOLLOW_RATE_LIMIT = 30
# Минимум матчей с достаточным числом голосов для рейтинга игроков.
PLAYER_LEADERBOARD_MIN_MATCHES = 3
FOLLOW_RATE_LIMIT_WINDOW_SECONDS = 60


class RegisterView(CreateView):
    """Регистрация с обязательной верификацией почты."""
    model = User
    form_class = UserRegistrationForm
    template_name = 'auth/register.html'
    success_url = reverse_lazy('users:verify_email_sent')

    def dispatch(self, request, *args, **kwargs):
        # Лимит до обработки формы.
        client_ip = get_client_ip(request)
        if request.method == 'POST' and client_ip:
            if is_rate_limited(f'register:{client_ip}', REGISTER_RATE_LIMIT, REGISTER_RATE_LIMIT_WINDOW_SECONDS):
                logger.warning(f"⚠️ Registration rate limit exceeded for IP {client_ip}")
                messages.error(request, 'Слишком много попыток регистрации. Попробуйте позже.')
                return redirect('users:register')
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        user = form.save(commit=False)
        user.is_verified = False  # Аккаунт не активирован до подтверждения почты
        user.set_password(form.cleaned_data['password1'])
        # Антифрод-данные регистрации.
        user.registration_ip = get_client_ip(self.request)
        user.registration_user_agent = self.request.META.get('HTTP_USER_AGENT', '')[:1000]
        user.save()
        UserXP.objects.get_or_create(user=user)

        # Приглашение друга и лига, по ссылке которой пришли (engagement).
        from engagement import friend_leagues, referrals
        from engagement.models import FriendLeague

        referrals.attach(self.request, user)
        league_code = self.request.session.pop('join_league_after_signup', None)
        league = FriendLeague.objects.filter(invite_code=league_code).first() if league_code else None
        if league:
            try:
                friend_leagues.join(user, league)
            except friend_leagues.LeagueError:
                pass

        # Аналитика: шаг воронки «регистрация». ref — партнёрская атрибуция из cookie.
        from django.core.signing import BadSignature

        from partners.services import REFERRAL_COOKIE_NAME

        # Cookie реферала подписанная; подделанную игнорируем.
        try:
            referral_slug = self.request.get_signed_cookie(REFERRAL_COOKIE_NAME, salt='partners.referral', default="")
        except BadSignature:
            referral_slug = ""
        track_event(
            EventName.USER_REGISTERED, request=self.request, user=user,
            properties={"ref": referral_slug} if referral_slug else None,
        )

        # Письмо верификации (асинхронно).
        try:
            from notifications.tasks import send_email_verification
            send_email_verification.delay(str(user.id), str(user.verification_token))
            logger.info(f"Verification email queued for {user.email}")
        except Exception as e:
            logger.error(f"Failed to queue verification email: {e}")
            # Не удалось поставить в очередь — отправляем синхронно.
            try:
                from notifications.tasks import _send_email_to_user
                site_url = getattr(settings, 'SITE_URL', 'http://127.0.0.1:8000')
                verify_url = f"{site_url}/users/verify-email/{user.verification_token}/"
                _send_email_to_user(user, 'Подтвердите email на DOPX', 'emails/verify_email.html', {'verify_url': verify_url}, force=True)
            except Exception as fallback_e:
                logger.critical(f"CRITICAL: Failed to send verification email synchronously: {fallback_e}")

        messages.success(self.request, 'Регистрация прошла успешно. Проверьте почту, чтобы активировать аккаунт.')
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = 'Регистрация · DOPX'
        context['panel_stats'] = get_auth_panel_stats()
        return context


class VerifyEmailSentView(TemplateView):
    template_name = 'auth/verify_email_sent.html'
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = 'Подтвердите email — DOPX'
        return context


class VerifyEmailView(View):
    """Подтверждение email по ссылке из письма. Лимит по IP — от перебора токенов."""
    def get(self, request, token):
        client_ip = get_client_ip(request)
        if client_ip and is_rate_limited(
            f'verify_email:{client_ip}', VERIFY_EMAIL_RATE_LIMIT, VERIFY_EMAIL_RATE_LIMIT_WINDOW_SECONDS
        ):
            logger.warning(f"⚠️ Verify-email rate limit exceeded for IP {client_ip}")
            messages.error(request, 'Слишком много попыток. Попробуйте позже.')
            return redirect('users:verify_email_invalid')
        try:
            user = User.objects.get(verification_token=token, is_verified=False)
            # Токен живёт 48 часов.
            token_age = timezone.now() - user.verification_token_created_at
            if token_age > timedelta(hours=48):
                messages.error(request, 'Ссылка для подтверждения устарела. Войдите с паролем, и мы пришлём новую.')
                return redirect('users:login')

            user.is_verified = True
            user.save(update_fields=['is_verified', 'updated_at'])
            from users.emails import mark_confirmed
            mark_confirmed(user)

            # Вход без authenticate(), поэтому backend указываем явно (их два).
            login(request, user, backend='django.contrib.auth.backends.ModelBackend')

            # Приветственное уведомление.
            Notification.objects.create(
                user=user,
                notification_type='welcome',
                title='👋 Добро пожаловать в DOPX!',
                message='Ваш аккаунт активирован. Оценивайте матчи и получайте достижения!',
                action_url='/matches/',
                is_read=False,
            )

            # Проверка бейджа «Первопроходец».
            try:
                from users.tasks import award_founder_badge_if_eligible
                award_founder_badge_if_eligible.delay(str(user.id))
            except Exception as e:
                logger.error(f"Failed to queue founder badge check: {e}")

            messages.success(request, 'Почта подтверждена. Добро пожаловать.')
            return redirect('core:home')
        except User.DoesNotExist:
            messages.error(request, 'Ссылка недействительна или уже использована.')
            return redirect('users:verify_email_invalid')


class VerifyEmailInvalidView(TemplateView):
    template_name = 'auth/verify_email_invalid.html'
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = 'Ошибка верификации — DOPX'
        return context


class LoginView(AuthLoginView):
    """Вход с проверкой подтверждения почты."""
    template_name = 'auth/login.html'
    authentication_form = UserLoginForm

    def form_valid(self, form):
        user = form.get_user()
        # Почта не подтверждена — не пускаем.
        if not user.is_verified:
            if is_rate_limited(
                f'resend_verification:{user.id}',
                RESEND_VERIFICATION_RATE_LIMIT, RESEND_VERIFICATION_RATE_LIMIT_WINDOW_SECONDS,
            ):
                messages.warning(self.request, 'Почта не подтверждена. Письмо уже отправлено, проверьте входящие и папку «Спам».')
                return redirect('users:login')
            user.refresh_verification_token()
            try:
                from notifications.tasks import send_email_verification
                send_email_verification.delay(str(user.id), str(user.verification_token))
            except Exception:
                logger.error("Failed to queue verification email on login", exc_info=True)
            messages.warning(self.request, 'Почта не подтверждена. Мы отправили новое письмо со ссылкой.')
            return redirect('users:login')

        response = super().form_valid(form)
        remember_me = self.request.POST.get('remember')
        if not remember_me:
            self.request.session.set_expiry(0)
        else:
            self.request.session.set_expiry(1209600)
        messages.success(self.request, f'С возвращением, {user.username}.')
        return response

    def get_success_url(self):
        # Только свой хост — иначе ?next= уводит на чужой сайт после входа.
        next_url = self.request.POST.get('next') or self.request.GET.get('next')
        if next_url and url_has_allowed_host_and_scheme(
            next_url, allowed_hosts={self.request.get_host()}, require_https=self.request.is_secure(),
        ):
            return next_url
        return reverse_lazy('core:home')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = 'Вход · DOPX'
        context['panel_stats'] = get_auth_panel_stats()
        return context


class LogoutView(LoginRequiredMixin, View):
    """Выход только POST — GET-ссылкой можно разлогинить с чужой страницы."""
    def post(self, request, *args, **kwargs):
        logout(request)
        messages.info(request, 'Вы вышли из аккаунта.')
        return redirect('core:home')


class ProfileView(LoginRequiredMixin, TemplateView):
    template_name = 'profile/dashboard.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        # «Оценок» — сумма оценок сущностей, «Матчей» — завершённые оценки матчей.
        # См. docs/adr/0024-profile-stats-ratings-vs-matches.md.
        total_ratings_given = sum(
            completed_only(rel.all()).count()
            for rel in (user.player_evaluations, user.team_evaluations, user.coach_evaluations, user.referee_evaluations)
        )
        stats = {
            'total_evaluations': user.evaluation_sessions.filter(status='completed').count(),
            'total_ratings_given': total_ratings_given,
            'total_players': completed_only(user.player_evaluations.all()).values('player').distinct().count(),
            'trust_score': round(user.trust_score, 2),
            'trust_level': user.get_trust_level(),
            'evaluation_streak': user.evaluation_streak,
            'total_matches': user.evaluation_sessions.filter(status='completed').count(),
            'prediction_streak': user.prediction_streak,
            'total_predictions': user.match_predictions.count(),
        }
        # Только завершённые сессии (ContextEvaluation есть и у брошенных).
        recent_evaluations = user.evaluation_sessions.filter(
            status='completed'
        ).select_related(
            'match__home_team', 'match__away_team', 'match__league'
        ).order_by('-completed_at')[:10]
        badges = UserBadge.objects.filter(user=user).order_by('-awarded_at')
        xp, _ = UserXP.objects.get_or_create(user=user)
        # Незавершённые сессии с ещё открытым голосованием.
        active_sessions = user.evaluation_sessions.filter(
            status__in=['started', 'in_progress'],
            match__voting_open_until__gte=timezone.now(),
            match__status='finished',
        ).select_related('match').order_by('-created_at')[:5]

        from engagement import season, streaks
        from core.templatetags.ui_extras import ru_plural
        from predictions.services import correct_predictions_count

        profile_url = self.request.build_absolute_uri(reverse('users:public_profile', args=[user.username]))
        brags = []
        if user.is_profile_public:
            def _brag(kind, number, title, text):
                brags.append({
                    'kind': kind, 'number': number, 'title': title, 'text': text,
                    'image': self.request.build_absolute_uri(reverse('engagement:brag_card', args=[user.username, kind])),
                    'story': reverse('engagement:story_card', args=[user.username, kind]),
                    # ?card= — превью ссылки с этой карточкой и подсветка плитки в профиле.
                    'url': f"{profile_url}?card={kind}",
                })
            hits = correct_predictions_count(user)
            if hits:
                _brag('predictions', hits, ru_plural(hits, 'прогноз сбылся,прогноза сбылись,прогнозов сбылись'), f'Мой прогноз сбылся уже {hits} раз на DOPX. Сможешь лучше?')
            day = streaks.state(user)
            if day and day['current'] >= 2:
                _brag('day_streak', day['current'], ru_plural(day['current'], 'день подряд,дня подряд,дней подряд'), f'{day["current"]} {ru_plural(day["current"], "день,дня,дней")} подряд на DOPX без пропусков!')
            pass_data = season.overview(user)
            if pass_data and pass_data['xp'] > 0:
                _brag('season', pass_data['level'], 'уровень абонемента', f'Уровень {pass_data["level"]} абонемента DOPX')

        context.update({
            'telegram_enabled': bool(settings.FAN_BOT_TOKEN and settings.FAN_BOT_USERNAME),
            'telegram_bot': settings.FAN_BOT_USERNAME.lstrip('@'),
            'brags': brags,
            'profile_share_url': profile_url,
            'user': user,
            'stats': stats,
            'recent_evaluations': recent_evaluations,
            'badges': badges,
            'xp': xp,
            'active_sessions': active_sessions,
            'page_title': f'Профиль — {user.username}'
        })
        return context


class BadgeCatalogView(LoginRequiredMixin, TemplateView):
    """Каталог достижений с отметкой «получено». Неполученные секретные — «???»."""
    template_name = 'users/badge_catalog.html'

    def get_context_data(self, **kwargs):
        from django.urls import reverse

        context = super().get_context_data(**kwargs)
        earned = {
            b.badge_type: b.awarded_at
            for b in UserBadge.objects.filter(user=self.request.user)
        }
        catalog = []
        for code, definition in BADGE_CATALOG.items():
            is_earned = code in earned
            catalog.append({
                'code': code,
                'name': definition.name if (is_earned or not definition.is_secret) else '???',
                'description': definition.description if (is_earned or not definition.is_secret) else 'Секретное достижение. Условия узнаете, когда получите его.',
                'rarity': definition.rarity,
                'is_secret': definition.is_secret,
                'earned': is_earned,
                'awarded_at': earned.get(code),
                # Ссылка на карточку — только для полученных достижений.
                'share_url': self.request.build_absolute_uri(
                    reverse('users:badge_share_card', args=[self.request.user.username, code])
                ) if is_earned else None,
            })
        catalog.sort(key=lambda b: (not b['earned'], -RARITY_ORDER.get(b['rarity'], 0), b['name']))
        # Легендарные — отдельной витриной.
        legendary_catalog = [b for b in catalog if b['rarity'] == 'legendary']
        other_catalog = [b for b in catalog if b['rarity'] != 'legendary']
        context.update({
            'catalog': catalog,
            'legendary_catalog': legendary_catalog,
            'other_catalog': other_catalog,
            'earned_count': len(earned),
            'total_count': len(BADGE_CATALOG),
            'page_title': 'Достижения — DOPX',
        })
        return context


def public_progress(user) -> dict:
    """Серия дней, абонемент и прогнозы для публичного профиля; cards — для каких есть brag-карточка."""
    from engagement import season, streaks
    from predictions.services import correct_predictions_count

    day = streaks.state(user)
    pass_data = season.overview(user)
    hits = correct_predictions_count(user)
    cards = set()
    if day and day['current'] >= 2:
        cards.add('day_streak')
    if pass_data and pass_data['xp'] > 0:
        cards.add('season')
    if hits:
        cards.add('predictions')
    return {'day': day, 'season': pass_data, 'hits': hits, 'cards': cards}


class PublicProfileView(TemplateView):
    """Публичный профиль любого пользователя (без приватных данных)."""
    template_name = 'users/public_profile.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        profile_user = get_object_or_404(User, username=kwargs['username'], is_active=True)
        # Владелец видит свой профиль даже скрытым. Остальным — 404.
        if not profile_user.is_profile_public and self.request.user != profile_user:
            from django.http import Http404
            raise Http404("Профиль скрыт владельцем")
        # Аналитика: просмотр чужого профиля.
        track_event(
            EventName.PROFILE_VIEWED, request=self.request,
            properties={"viewed_username": profile_user.username},
        )
        # См. ProfileView.
        total_ratings_given = sum(
            completed_only(rel.all()).count()
            for rel in (profile_user.player_evaluations, profile_user.team_evaluations, profile_user.coach_evaluations, profile_user.referee_evaluations)
        )
        stats = {
            'total_evaluations': profile_user.evaluation_sessions.filter(status='completed').count(),
            'total_ratings_given': total_ratings_given,
            'total_players': completed_only(profile_user.player_evaluations.all()).values('player').distinct().count(),
            'trust_score': round(profile_user.trust_score, 2),
            'trust_level': profile_user.get_trust_level(),
            'evaluation_streak': profile_user.evaluation_streak,
            'total_matches': profile_user.evaluation_sessions.filter(status='completed').count(),
            'prediction_streak': profile_user.prediction_streak,
            'total_predictions': profile_user.match_predictions.count(),
        }
        badges = list(UserBadge.objects.filter(user=profile_user).order_by('-awarded_at'))
        # Секретные бейджи показываем только владельцу.
        if self.request.user != profile_user:
            badges = [b for b in badges if not b.is_secret]
        xp, _ = UserXP.objects.get_or_create(user=profile_user)
        recent_evaluations = profile_user.evaluation_sessions.filter(
            status='completed'
        ).select_related('match__home_team', 'match__away_team', 'match__league').order_by('-completed_at')[:10]

        progress = public_progress(profile_user)
        card = self.request.GET.get('card', '')
        if card in progress['cards']:
            context['og_image'] = self.request.build_absolute_uri(
                reverse('engagement:brag_card', args=[profile_user.username, card]))
            context['meta_description'] = {
                'day_streak': lambda: f"{progress['day']['current']} дн. подряд на DOPX",
                'season': lambda: f"Уровень {progress['season']['level']} абонемента DOPX",
                'predictions': lambda: f"Прогноз сбылся {progress['hits']} раз на DOPX",
            }[card]()

        context.update({
            'progress': progress,
            'highlight_card': card,
            'profile_user': profile_user,
            'stats': stats,
            'badges': badges,
            'xp': xp,
            'recent_evaluations': recent_evaluations,
            'is_own_profile': self.request.user == profile_user,
            'page_title': f'{profile_user.username} — DOPX',
        })
        return context


class BadgeShareCardView(View):
    """PNG-карточка достижения для шеринга (редирект на закэшированный файл).
    Владелец видит свою всегда, чужую — только при публичном профиле.
    Секретные достижения чужим не отдаём.
    """

    def get(self, request, username, code):
        from django.core.files.storage import default_storage
        from django.http import Http404
        from core.services.share_cards import build_badge_share_card
        from users.badges import get_badge_definition

        target_user = get_object_or_404(User, username=username, is_active=True)
        is_owner = request.user.is_authenticated and request.user == target_user
        if not is_owner and not target_user.is_profile_public:
            raise Http404("Профиль скрыт владельцем")

        definition = get_badge_definition(code)
        if definition is None:
            raise Http404("Неизвестный код достижения")
        if definition.is_secret and not is_owner:
            raise Http404("Секретное достижение")

        user_badge = get_object_or_404(UserBadge, user=target_user, badge_type=code)

        path = build_badge_share_card(
            username=target_user.username,
            badge_code=code,
            badge_name=definition.name,
            badge_description=definition.description,
            rarity=definition.rarity,
            is_secret=definition.is_secret,
            awarded_at=user_badge.awarded_at,
        )
        return redirect(default_storage.url(path))


class ProfileEditView(LoginRequiredMixin, UpdateView):
    model = User
    form_class = UserProfileForm
    template_name = 'users/profile_edit.html'
    success_url = reverse_lazy('users:profile')

    def get_object(self):
        # Берём свежий объект из БД: OTPMiddleware подменяет request.user.is_verified
        # на функцию, и save() падает на BooleanField.
        return User.objects.get(pk=self.request.user.pk)

    def form_valid(self, form):
        # Новая почта не применяется сразу: ждёт подтверждения по ссылке (users/emails.py), старая работает.
        old_email = User.objects.get(pk=self.object.pk).email
        new_email = form.cleaned_data.get('email')
        email_changed = bool(new_email) and new_email.lower() != old_email.lower()
        if email_changed:
            form.instance.email = old_email

        if form.cleaned_data.get('delete_avatar') and self.object.avatar:
            self.object.avatar.delete(save=False)
            self.object.avatar = None

        response = super().form_valid(form)
        if self.object.avatar:
            from engagement.quests import record

            record(self.object, "avatar_set", "avatar")

        if email_changed:
            from users.emails import request_change
            request_change(self.object, new_email)
            messages.success(self.request, f'Профиль обновлён. Почта сменится после подтверждения: мы отправили ссылку на {new_email}.')
        else:
            messages.success(self.request, 'Профиль обновлён.')
        return response

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
        messages.success(self.request, 'Пароль изменён.')
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = 'Изменить пароль — DOPX'
        return context


class PasswordResetViewCustom(PasswordResetView):
    """Сброс пароля с лимитом по IP (иначе флуд письмами на чужие адреса)."""
    template_name = 'auth/password_reset.html'
    email_template_name = 'emails/password_reset_email.txt'
    html_email_template_name = 'emails/password_reset_email.html'
    subject_template_name = 'emails/password_reset_subject.txt'
    success_url = reverse_lazy('users:password_reset_done')
    form_class = CustomPasswordResetForm
    # site_url для общих шапки/подвала письма.
    extra_email_context = {'site_url': getattr(settings, 'SITE_URL', 'https://dopx.kz')}

    def dispatch(self, request, *args, **kwargs):
        # Лимит до валидации формы.
        client_ip = get_client_ip(request)
        if request.method == 'POST' and client_ip:
            if is_rate_limited(
                f'password_reset:{client_ip}', PASSWORD_RESET_RATE_LIMIT, PASSWORD_RESET_RATE_LIMIT_WINDOW_SECONDS
            ):
                logger.warning(f"⚠️ Password reset rate limit exceeded for IP {client_ip}")
                messages.error(request, 'Слишком много попыток. Попробуйте позже.')
                return redirect('users:password_reset')
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        messages.success(self.request, 'Инструкция отправлена на почту.')
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
        # Ключи — из User.DEFAULT_NOTIFICATION_SETTINGS.
        user._notification_settings = {
            key: form.cleaned_data.get(key, True)
            for key in User.DEFAULT_NOTIFICATION_SETTINGS
        }
        user.save(update_fields=['_notification_settings', 'updated_at'])
        user.refresh_from_db()
        messages.success(self.request, 'Настройки уведомлений сохранены.')
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = 'Настройки уведомлений — DOPX'
        from django.conf import settings as dj_settings
        from fanbot.models import TelegramAccount
        context['telegram_account'] = TelegramAccount.objects.filter(user=self.request.user).first()
        context['telegram_enabled'] = bool(dj_settings.FAN_BOT_TOKEN and dj_settings.FAN_BOT_USERNAME)
        context['telegram_bot'] = dj_settings.FAN_BOT_USERNAME.lstrip('@')
        # Есть ли активная push-подписка хоть на одном устройстве.
        push_subscriptions = self.request.user.push_subscriptions.order_by('-created_at')
        context['has_push_subscription'] = push_subscriptions.exists()
        # Список подписанных устройств для карточки «Ваши устройства».
        context['push_subscriptions'] = push_subscriptions
        return context


class UserLeaderboardView(ListView):
    """Лидерборд пользователей. SORT_OPTIONS — доступные срезы, ?sort= выбирает срез,
    ?city= — фильтр по городу.
    """
    model = User
    template_name = 'users/leaderboard.html'
    context_object_name = 'users'
    paginate_by = 20

    SORT_OPTIONS = {
        'trust': {'label': 'Доверие', 'icon': 'ti-shield-check', 'order_by': ('-trust_score', '-eval_count')},
        'active': {'label': 'Активность', 'icon': 'ti-flame', 'order_by': ('-total_evaluations', '-trust_score')},
        'streak': {'label': 'Серия оценок', 'icon': 'ti-bolt', 'order_by': ('-evaluation_streak', '-trust_score')},
    }
    DEFAULT_SORT = 'trust'

    def get_paginate_by(self, queryset):
        # Размер страницы — из настроек платформы.
        return get_setting("user_leaderboard_page_size", self.paginate_by)

    def _sort_key(self):
        key = self.request.GET.get('sort', self.DEFAULT_SORT)
        return key if key in self.SORT_OPTIONS else self.DEFAULT_SORT

    def _base_queryset(self):
        # select_related('xp') — уровень выводится в каждой строке.
        # Оценка = завершённая сессия вайзарда; брошенные на середине не считаются.
        qs = User.objects.filter(is_active=True, is_verified=True).exclude(service_accounts_q()).select_related('xp').annotate(
            eval_count=Count('evaluation_sessions', filter=Q(evaluation_sessions__status='completed'), distinct=True)
        ).filter(eval_count__gte=1)
        # ?city= — точное совпадение (значение из выпадающего списка).
        city = self.request.GET.get('city', '').strip()
        if city:
            qs = qs.filter(city__iexact=city)
        return qs

    def get_queryset(self):
        order_by = self.SORT_OPTIONS[self._sort_key()]['order_by']
        return self._base_queryset().order_by(*order_by)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = 'Рейтинг пользователей — DOPX'
        context['selected_city'] = self.request.GET.get('city', '').strip()
        context['sort_options'] = self.SORT_OPTIONS
        context['selected_sort'] = self._sort_key()
        # Только города, которые реально есть у пользователей.
        context['available_cities'] = (
            User.objects.filter(is_active=True, is_verified=True).exclude(service_accounts_q())
            .exclude(city='').values_list('city', flat=True).distinct().order_by('city')
        )

        # «Твоя позиция и соседи»: ранжируем RowNumber() по тому же порядку и фильтру.
        # Фильтровать по window-аннотации Django не даёт, поэтому берём список (id, rank)
        # и вырезаем окно в Python.
        context['my_rank_neighbors'] = []
        user = self.request.user
        if user.is_authenticated:
            order_by = self.SORT_OPTIONS[self._sort_key()]['order_by']
            order_exprs = [F(f[1:]).desc() if f.startswith('-') else F(f).asc() for f in order_by]
            ranked_list = list(
                self._base_queryset()
                .annotate(rank=Window(expression=RowNumber(), order_by=order_exprs))
                .order_by('rank')
                .values_list('id', 'rank')
            )
            rank_by_id = dict(ranked_list)
            my_rank = rank_by_id.get(user.id)
            if my_rank is not None:
                # Блок показываем, только если пользователя нет на текущей странице.
                page_size = self.get_paginate_by(None)
                page_number = context['page_obj'].number if context.get('page_obj') else 1
                visible_range = range((page_number - 1) * page_size + 1, page_number * page_size + 1)
                if my_rank not in visible_range:
                    neighbor_ids = [uid for uid, r in ranked_list if my_rank - 2 <= r <= my_rank + 2]
                    users_by_id = User.objects.filter(id__in=neighbor_ids).select_related('xp').in_bulk()
                    context['my_rank_neighbors'] = [
                        (users_by_id[uid], r, uid == user.id)
                        for uid, r in ranked_list
                        if my_rank - 2 <= r <= my_rank + 2 and uid in users_by_id
                    ]
        return context


class CityLeaderboardView(TemplateView):
    """Битва городов: рейтинг городов по оценкам на пользователя. ?period= — month/season/all."""
    template_name = 'users/city_leaderboard.html'

    def get_context_data(self, **kwargs):
        from users.city_stats import BATTLE_PERIODS, city_battle, city_min_users

        context = super().get_context_data(**kwargs)
        period = self.request.GET.get('period', 'month')
        if period not in BATTLE_PERIODS:
            period = 'month'
        rows = city_battle(period)
        context.update({
            'page_title': 'Битва городов — DOPX',
            'cities': rows,
            'periods': BATTLE_PERIODS,
            'selected_period': period,
            'min_users': city_min_users(),
            'my_city': getattr(self.request.user, 'city', '') if self.request.user.is_authenticated else '',
        })
        return context


class PlayerLeaderboardView(ListView):
    template_name = 'players/leaderboard.html'
    context_object_name = 'players'
    paginate_by = 20

    def get_paginate_by(self, queryset):
        # Размер страницы — из настроек платформы.
        return get_setting("player_leaderboard_page_size", self.paginate_by)

    def get_queryset(self):
        from players.models import Player
        from aggregates.services import min_votes_for_display, published_q

        # В рейтинг идут только матчи с достаточным числом голосов, и нужно несколько таких матчей —
        # иначе наверху окажется игрок с одной «десяткой» от одного голоса.
        counted = Q(match_aggregates__total_votes__gte=min_votes_for_display()) & published_q('match_aggregates__match__')
        # ?league= — внутри того же фильтра агрегации (у игрока нет FK на лигу).
        league_id = self.request.GET.get('league', '').strip()
        if league_id:
            import uuid
            try:
                counted &= Q(match_aggregates__match__league_id=uuid.UUID(league_id))
            except ValueError:
                pass  # мусорный ?league= — без фильтра
        min_matches = get_setting("player_leaderboard_min_matches", PLAYER_LEADERBOARD_MIN_MATCHES)
        return Player.objects.filter(is_active=True).annotate(
            avg_performance=vote_weighted_avg(
                'match_aggregates__performance_score', 'match_aggregates__total_votes', filter=counted,
            ),
            total_matches=Count('match_aggregates', filter=counted, distinct=True),
            total_votes=Sum('match_aggregates__total_votes', filter=counted),
        ).filter(
            avg_performance__isnull=False,
            total_matches__gte=min_matches,
        ).order_by('-avg_performance', '-total_votes')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = 'Рейтинг игроков — DOPX'
        from leagues.models import League
        context['selected_league'] = self.request.GET.get('league', '').strip()
        context['available_leagues'] = League.objects.order_by('name')
        return context


@require_POST
@login_required
def toggle_follow(request, target_type, target_id):
    """Подписаться/отписаться на игрока или команду. Возвращает HTMX-партиал кнопки.
    Лимит 30/мин на пользователя; при превышении 429 — кнопка просто не обновится.
    """
    from django.http import Http404

    if is_rate_limited(f'toggle_follow:{request.user.id}', FOLLOW_RATE_LIMIT, FOLLOW_RATE_LIMIT_WINDOW_SECONDS):
        return HttpResponse(status=429)

    if target_type == 'player':
        from players.models import Player
        target = get_object_or_404(Player, id=target_id)
        lookup = {'player': target}
    elif target_type == 'team':
        from teams.models import Team
        target = get_object_or_404(Team, id=target_id)
        lookup = {'team': target}
    else:
        raise Http404("Неизвестный тип подписки")

    existing = Follow.objects.filter(user=request.user, **lookup).first()
    if existing:
        existing.delete()
        following = False
    else:
        Follow.objects.create(user=request.user, **lookup)
        following = True

    return render(request, 'users/_follow_button.html', {
        'target_type': target_type,
        'target_id': target_id,
        'following': following,
    })


@require_POST
@login_required
def push_subscribe(request):
    """Сохраняет push-подписку браузера (update_or_create по endpoint)."""
    import json

    from users.models import PushSubscription

    try:
        data = json.loads(request.body)
        endpoint = data['endpoint']
        keys = data['keys']
        p256dh = keys['p256dh']
        auth = keys['auth']
    except (json.JSONDecodeError, KeyError, TypeError):
        return JsonResponse({'ok': False, 'error': 'invalid payload'}, status=400)

    _sub, created = PushSubscription.objects.update_or_create(
        endpoint=endpoint,
        defaults={
            'user': request.user,
            'p256dh': p256dh,
            'auth': auth,
            'user_agent': request.META.get('HTTP_USER_AGENT', '')[:255],
        },
    )
    # created — фронт перерисует список устройств.
    return JsonResponse({'ok': True, 'created': created})


@login_required
def push_devices_partial(request):
    """Список устройств с push — перерисовка без перезагрузки страницы."""
    return render(request, 'users/_push_devices.html', {
        'push_subscriptions': request.user.push_subscriptions.order_by('-created_at'),
    })


@require_POST
@login_required
def push_unsubscribe(request):
    """Удаляет push-подписку по endpoint."""
    import json

    from users.models import PushSubscription

    try:
        data = json.loads(request.body)
        endpoint = data['endpoint']
    except (json.JSONDecodeError, KeyError, TypeError):
        return JsonResponse({'ok': False, 'error': 'invalid payload'}, status=400)

    PushSubscription.objects.filter(user=request.user, endpoint=endpoint).delete()
    return JsonResponse({'ok': True})


@require_POST
@login_required
def push_revoke_device(request, subscription_id):
    """Отключить конкретное устройство по id подписки (из настроек, с любого устройства)."""
    from users.models import PushSubscription

    deleted, _ = PushSubscription.objects.filter(user=request.user, id=subscription_id).delete()
    if deleted:
        messages.success(request, 'Устройство отключено от push-уведомлений.')
    return redirect('users:notification_settings')

@login_required
@require_POST
def report(request, kind, key):
    """Жалоба на профиль (kind=user, key=ник) или лигу друзей (kind=league, key=код приглашения)."""
    from engagement.models import FriendLeague
    from users import reports

    if kind == "user":
        target = {"target_user": get_object_or_404(User, username=key, is_active=True)}
    elif kind == "league":
        target = {"league": get_object_or_404(FriendLeague, invite_code=key)}
    else:
        raise Http404
    try:
        reports.create(request.user, reason=request.POST.get("reason", ""), comment=request.POST.get("comment", ""), **target)
        messages.success(request, "Спасибо! Модераторы посмотрят жалобу.")
    except reports.ReportError as e:
        messages.error(request, str(e))
    back = request.POST.get("next", "")
    if not url_has_allowed_host_and_scheme(back, allowed_hosts={request.get_host()}):
        back = "/"
    return redirect(back)


@login_required
def complete_profile(request):
    """Город и подтверждённая почта (без них оценки и прогнозы закрыты); для пустого аккаунта из Telegram —
    объединение с уже существующим аккаунтом по логину и паролю."""
    from fanbot import services as tg_services
    from fanbot.views import WELCOME_SKIP_KEY
    from users import emails
    from users.forms import UserLoginForm
    from users.forms_profile import CompleteProfileForm

    nxt = request.POST.get("next") or request.GET.get("next", "")
    if not url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}):
        nxt = reverse("users:profile")
    # Свежий объект: OTPMiddleware подменяет request.user.is_verified функцией, такой save() падает.
    user = User.objects.get(pk=request.user.pk)
    can_merge = hasattr(user, "telegram") and tg_services._empty_telegram_only(user)
    form = CompleteProfileForm(user=user)
    merge_form = UserLoginForm(request)
    action = request.POST.get("action")

    if request.method == "POST" and action == "skip":
        request.session[WELCOME_SKIP_KEY] = True
        return redirect(nxt)
    if request.method == "POST" and action == "resend" and user.pending_email:
        emails.request_change(user, user.pending_email)
        messages.success(request, f"Письмо отправлено ещё раз на {user.pending_email}.")
        return redirect(f"{reverse('users:complete_profile')}?next={nxt}")
    if request.method == "POST" and action == "merge" and can_merge:
        merge_form = UserLoginForm(request, data=request.POST)
        if merge_form.is_valid():
            target = merge_form.get_user()
            error = tg_services.merge_into(request, target)
            if not error:
                messages.success(request, f"Готово: аккаунты объединены. Теперь вход через Telegram открывает {target.username}.")
                return redirect(nxt)
            messages.error(request, error)
    if request.method == "POST" and action == "profile":
        form = CompleteProfileForm(request.POST, user=user)
        if form.is_valid():
            user.city = form.cleaned_data["city"]
            user.save(update_fields=["city", "updated_at"])
            email = form.cleaned_data["email"]
            if email.lower() != user.email.lower():
                emails.request_change(user, email)
                messages.success(request, f"Город сохранён. Мы отправили письмо на {email}, перейдите по ссылке, чтобы подтвердить почту.")
                return redirect(f"{reverse('users:complete_profile')}?next={nxt}")
            if user.email_verified_at is None:
                emails.request_change(user, email)  # та же почта, но не подтверждена — шлём ссылку
                messages.success(request, f"Город сохранён. Подтвердите почту по ссылке из письма на {email}.")
                return redirect(f"{reverse('users:complete_profile')}?next={nxt}")
            messages.success(request, "Профиль заполнен. Спасибо!")
            return redirect(nxt)

    return render(request, "users/complete_profile.html", {
        "page_title": "Заполните профиль — DOPX", "form": form, "merge_form": merge_form, "can_merge": can_merge,
        "next": nxt, "show_merge": action == "merge" or "email" in form.errors,
        "missing": emails.missing_fields(user), "complete": emails.profile_complete(user),
    })


def confirm_email_change(request, token):
    """Ссылка из письма: новая почта подтверждена и становится основной."""
    from users import emails

    client_ip = get_client_ip(request)
    if client_ip and is_rate_limited(f'confirm_email:{client_ip}', VERIFY_EMAIL_RATE_LIMIT, VERIFY_EMAIL_RATE_LIMIT_WINDOW_SECONDS):
        messages.error(request, 'Слишком много попыток. Попробуйте позже.')
        return redirect('core:home')
    user, error = emails.confirm_change(token)
    if error:
        messages.error(request, error)
        return redirect('users:complete_profile' if request.user.is_authenticated else 'users:login')
    messages.success(request, f"Почта {user.email} подтверждена.")
    return redirect('users:profile' if request.user.is_authenticated else 'users:login')
