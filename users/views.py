# users/views.py
"""
RegisterView: rate-limit по IP через `core.utils.is_rate_limited` (не более
REGISTER_RATE_LIMIT регистраций с одного IP за REGISTER_RATE_LIMIT_WINDOW_
SECONDS), сохранение registration_ip/registration_user_agent на созданном
пользователе — антифрод-данные для IP-кластерного анализа (users/tasks.py::
detect_ip_clusters_task). Honeypot/time-trap проверяются самой формой
(users/forms.py) через стандартный form_invalid.

VerifyEmailView: после верификации ставит в очередь
users.tasks.award_founder_badge_if_eligible (бейдж «Первопроходец»).

NotificationSettingsView.form_valid: словарь настроек строится из
User.DEFAULT_NOTIFICATION_SETTINGS.keys(), не хардкодом — новое поле в форме
подхватывается без правки этого метода.
"""
from __future__ import annotations

from django.http import JsonResponse, HttpResponse
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

from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST

from analytics.models import EventName
from analytics.services import track_event
from core.utils import get_auth_panel_stats, get_client_ip, is_rate_limited
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

# password-reset, verify-email — по IP: анонимные эндпоинты, до request.user
# добраться нельзя. toggle_follow, react_to_event — по user.id: за декоратором
# @login_required, IP менее показателен (NAT/мобильные сети), а сам факт
# аутентификации уже отсекает анонимный флуд.
PASSWORD_RESET_RATE_LIMIT = 5
PASSWORD_RESET_RATE_LIMIT_WINDOW_SECONDS = 60 * 60  # 1 час
VERIFY_EMAIL_RATE_LIMIT = 20
VERIFY_EMAIL_RATE_LIMIT_WINDOW_SECONDS = 60 * 10  # 10 минут
FOLLOW_RATE_LIMIT = 30
FOLLOW_RATE_LIMIT_WINDOW_SECONDS = 60


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
        # Антифрод-данные регистрации, см. докстринг модуля.
        user.registration_ip = get_client_ip(self.request)
        user.registration_user_agent = self.request.META.get('HTTP_USER_AGENT', '')[:1000]
        user.save()
        # Создаем базовые профили
        UserXP.objects.get_or_create(user=user)

        # Продуктовая аналитика: первый шаг воронки "визит → регистрация →
        # первая оценка" (см. analytics/selectors.py::registration_funnel).
        # Здесь без transaction.on_commit — у RegisterView нет обёртывающего
        # transaction.atomic(), user.save() уже закоммичен к этому моменту.
        # ref — партнёрская атрибуция (partners/services.py::REFERRAL_COOKIE_NAME):
        # если пользователь пришёл по /go/<slug>/ за последние 30 дней, здесь
        # видно, что визит по партнёрской ссылке КОНВЕРТИРОВАЛСЯ в регистрацию,
        # а не просто засчитался как переход.
        from django.core.signing import BadSignature

        from partners.services import REFERRAL_COOKIE_NAME

        # БАГ, КОТОРЫЙ ТУТ БЫЛ: cookie читалась как обычная (COOKIES.get) —
        # см. partners/views.py::PartnerReferralRedirectView, где её теперь
        # ставят через set_signed_cookie(salt='partners.referral'). Здесь
        # соответственно читаем через get_signed_cookie с тем же salt;
        # BadSignature (кто-то подделал/отредактировал cookie вручную —
        # значение не совпадает с подписью) просто игнорируем, как будто
        # cookie не было — не должно ронять регистрацию из-за чужого мусора
        # в cookies.
        try:
            referral_slug = self.request.get_signed_cookie(REFERRAL_COOKIE_NAME, salt='partners.referral', default="")
        except BadSignature:
            referral_slug = ""
        track_event(
            EventName.USER_REGISTERED, request=self.request, user=user,
            properties={"ref": referral_slug} if referral_slug else None,
        )

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
    """
    Обработка клика по ссылке из письма для верификации.

    Токен — непредсказуемый UUID, поэтому основная угроза не подбор
    конкретного токена, а перебор случайных UUID с одного IP в расчёте
    когда-нибудь попасть в чужой активный токен (или просто нагрузить
    User.objects.get() запросами). Лимит по IP, а не по токену/юзеру —
    до аутентификации никакого юзера ещё нет.
    """
    def get(self, request, token):
        client_ip = get_client_ip(request)
        if client_ip and is_rate_limited(
            f'verify_email:{client_ip}', VERIFY_EMAIL_RATE_LIMIT, VERIFY_EMAIL_RATE_LIMIT_WINDOW_SECONDS
        ):
            logger.warning(f"⚠️ Verify-email rate limit exceeded for IP {client_ip}")
            messages.error(request, '⚠️ Слишком много попыток. Попробуйте позже.')
            return redirect('users:verify_email_invalid')
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

            # Автоматический вход по ссылке из письма, без вызова authenticate()
            # (пароль тут не проверяется) — user.backend не выставлен сам, а
            # AUTHENTICATION_BACKENDS содержит два бэкенда (axes + ModelBackend),
            # так что login() без явного backend бросает ValueError.
            login(request, user, backend='django.contrib.auth.backends.ModelBackend')

            # Приветственное уведомление (критическое, не отключается)
            Notification.objects.create(
                user=user,
                notification_type='welcome',
                title='👋 Добро пожаловать в DOPX!',
                message='Ваш аккаунт активирован. Оценивайте матчи и получайте достижения!',
                action_url='/matches/',
                is_read=False,
            )

            # Разовая проверка бейджа «Первопроходец», асинхронно.
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
        context['page_title'] = 'Вход · DOPX'
        context['panel_stats'] = get_auth_panel_stats()
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
        # НАЙДЕНО (2026-09-01, жалоба пользователя: "непонятно что за
        # оценок, что за матчей, что за игроков" в блоке статистики
        # профиля): "Оценок" (total_evaluations) и "Матчей" (total_matches)
        # раньше показывали ОДНО И ТО ЖЕ число — оба считают "сколько раз
        # вайзард оценки матча пройден до конца" (total_evaluations — через
        # аккумулятор на User, total_matches — через COUNT завершённых
        # EvaluationSession), просто двумя разными путями к одному и тому
        # же факту. Две одинаковые цифры под разными подписями выглядят как
        # баг, даже когда обе технически верны. Заменяем "Оценок" на
        # РЕАЛЬНО другую метрику — сколько отдельных оценок (игрокам,
        # командам, тренерам, судьям суммарно) поставлено за все матчи, а
        # не сколько матчей оценено целиком (это остаётся за "Матчей").
        total_ratings_given = (
            user.player_evaluations.count()
            + user.team_evaluations.count()
            + user.coach_evaluations.count()
            + user.referee_evaluations.count()
        )
        stats = {
            'total_evaluations': user.total_evaluations,
            'total_ratings_given': total_ratings_given,
            'total_players': user.player_evaluations.values('player').distinct().count(),
            'trust_score': round(user.trust_score, 2),
            'trust_level': user.get_trust_level(),
            'evaluation_streak': user.evaluation_streak,
            'total_matches': user.evaluation_sessions.filter(status='completed').count(),
            # НОВОЕ (retention loop "Серии", 2026-08-21) — прямой аналог
            # evaluation_streak выше, для прогнозов 1X2 (predictions app).
            'prediction_streak': user.prediction_streak,
            'total_predictions': user.match_predictions.count(),
        }
        # user.context_evaluations не годится источником: ContextEvaluation
        # создаётся уже на первом шаге вайзарда (evaluations/views.py::
        # EvaluateContextView.form_valid), т.е. существует и для брошенных
        # на полпути оценок. Берём только реально завершённые сессии.
        recent_evaluations = user.evaluation_sessions.filter(
            status='completed'
        ).select_related(
            'match__home_team', 'match__away_team', 'match__league'
        ).order_by('-completed_at')[:10]
        # rarity/is_secret — properties поверх users/badges.py::BADGE_CATALOG,
        # доступны в шаблоне как badge.rarity / badge.is_secret / badge.description.
        badges = UserBadge.objects.filter(user=user).order_by('-awarded_at')
        xp, _ = UserXP.objects.get_or_create(user=user)
        # Та же логика, что и на главной (core/views.py::HomeView): не
        # показываем сессии, чью voting_open_until уже прошло — Continue
        # вёл бы в тупик, EvaluationWizardMixin.check_voting_access всё
        # равно заблокирует первый шаг.
        active_sessions = user.evaluation_sessions.filter(
            status__in=['started', 'in_progress'],
            match__voting_open_until__gte=timezone.now(),
            match__status='finished',
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


class BadgeCatalogView(LoginRequiredMixin, TemplateView):
    """
    Полный каталог достижений (`users/badges.py::BADGE_CATALOG`) с отметкой
    "получено/не получено". Секретные достижения, которые пользователь ещё
    не получил, показываются как "???" — иначе теряется смысл секретности.
    """
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
                'description': definition.description if (is_earned or not definition.is_secret) else 'Секретное достижение — условия получения не раскрываются заранее.',
                'rarity': definition.rarity,
                'is_secret': definition.is_secret,
                'earned': is_earned,
                'awarded_at': earned.get(code),
                # Ссылка на премиальную PNG-карточку (BadgeShareCardView) —
                # только для полученных ачивок, иначе кнопка "Поделиться" вела
                # бы на 404 (карточка требует существующую UserBadge-запись).
                'share_url': self.request.build_absolute_uri(
                    reverse('users:badge_share_card', args=[self.request.user.username, code])
                ) if is_earned else None,
            })
        catalog.sort(key=lambda b: (not b['earned'], -RARITY_ORDER.get(b['rarity'], 0), b['name']))
        # НОВОЕ (2026-09-01, "супер ультра" достижения): легендарные выводятся
        # отдельной витриной над обычной сеткой — разбиваем список тут, в
        # Python, а не городим подсчёт "первого легендарного элемента" в
        # шаблоне через forloop (ненадёжно и нечитаемо при вложенных {% for %}).
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


class PublicProfileView(TemplateView):
    """
    Публичный (только для чтения) профиль ЛЮБОГО пользователя по username.

    ProfileView всегда рендерит только request.user — для перехода в чужой
    профиль (со страницы рейтинга) нужен отдельный маршрут без параметров.
    Набор данных минимальный и без приватной информации (email, настройки,
    кнопки редактирования — только в ProfileView, только для себя).
    """
    template_name = 'users/public_profile.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        profile_user = get_object_or_404(User, username=kwargs['username'], is_active=True)
        # is_profile_public не применяется к владельцу — он видит свою
        # страницу даже при выключенной видимости. 404, а не редирект на
        # логин — не палим гостю разницу между "не существует" и "скрыт".
        if not profile_user.is_profile_public and self.request.user != profile_user:
            from django.http import Http404
            raise Http404("Профиль скрыт владельцем")
        # Продуктовая аналитика: кто-то открыл публичный профиль — вход в
        # воронку "лидерборд/шер → чужой профиль → регистрация".
        track_event(
            EventName.PROFILE_VIEWED, request=self.request,
            properties={"viewed_username": profile_user.username},
        )
        # См. подробный комментарий у той же конструкции в ProfileView
        # (тот же баг: "Оценок" и "Матчей" показывали одно и то же число).
        total_ratings_given = (
            profile_user.player_evaluations.count()
            + profile_user.team_evaluations.count()
            + profile_user.coach_evaluations.count()
            + profile_user.referee_evaluations.count()
        )
        stats = {
            'total_evaluations': profile_user.total_evaluations,
            'total_ratings_given': total_ratings_given,
            'total_players': profile_user.player_evaluations.values('player').distinct().count(),
            'trust_score': round(profile_user.trust_score, 2),
            'trust_level': profile_user.get_trust_level(),
            'evaluation_streak': profile_user.evaluation_streak,
            'total_matches': profile_user.evaluation_sessions.filter(status='completed').count(),
            'prediction_streak': profile_user.prediction_streak,
            'total_predictions': profile_user.match_predictions.count(),
        }
        badges = list(UserBadge.objects.filter(user=profile_user).order_by('-awarded_at'))
        # Секретные бейджи не палим до их получения посторонним — только
        # владельцу профиля (см. UserBadge.is_secret / users/badges.py).
        if self.request.user != profile_user:
            badges = [b for b in badges if not b.is_secret]
        xp, _ = UserXP.objects.get_or_create(user=profile_user)
        recent_evaluations = profile_user.evaluation_sessions.filter(
            status='completed'
        ).select_related('match__home_team', 'match__away_team', 'match__league').order_by('-completed_at')[:10]

        context.update({
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
    """
    /u/<username>/badges/<code>/card.png — премиальная PNG-карточка
    достижения для шеринга (продуктовый запрос 2026-09-01, "красивая супер
    по дизайну карточка премиальная"). Тот же редирект-на-закэшированный-PNG
    паттерн, что и MatchShareCardView/StreakShareCardView (core/views.py) и
    player_season_recap_card (players/views.py).

    Доступ: владелец видит свою карточку всегда, независимо от
    is_profile_public; чужой профиль — только если is_profile_public=True
    (тот же принцип, что PublicProfileView.get_context_data выше). ВАЖНО:
    StreakShareCardView НЕ делает исключение для владельца приватного
    профиля (`get_object_or_404(User, username=username,
    is_profile_public=True)`) — это существующая недоработка, которую мы
    сознательно НЕ повторяем здесь: иначе пользователь, выключивший
    публичность профиля, не смог бы поделиться даже собственным
    достижением.

    Секретные ачивки (`is_secret`) чужому посетителю не показываем и не
    рендерим по прямой ссылке на код, даже если профиль публичный — тот же
    принцип фильтрации, что для `badges` в PublicProfileView выше.
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
        return self.request.user

    def form_valid(self, form):
        # БАГ, КОТОРЫЙ ТУТ БЫЛ (найден полным аудитом, август 2026): смена
        # email в этой форме сохранялась без сброса is_verified — пользователь
        # мог вписать чужой/недоступный ему адрес и его аккаунт остался бы
        # помечен как "верифицирован" для НЕподтверждённого нового email
        # (is_verified=True рассылки/публичный листинг is_verified=True
        # используют этот флаг как сигнал доверия). Старый email берём
        # из БД, а не из self.object — ModelForm уже переписал
        # self.object.email новым значением на этапе form.is_valid()
        # (_post_clean), до вызова form_valid().
        old_email = User.objects.get(pk=self.object.pk).email
        new_email = form.cleaned_data.get('email')
        email_changed = new_email and new_email != old_email

        if form.cleaned_data.get('delete_avatar') and self.object.avatar:
            self.object.avatar.delete(save=False)
            self.object.avatar = None

        if email_changed:
            self.object.is_verified = False

        response = super().form_valid(form)

        if email_changed:
            try:
                from notifications.tasks import send_email_verification
                send_email_verification.delay(str(self.object.id), str(self.object.verification_token))
                logger.info(f"Re-verification email queued for {self.object.email} (email changed)")
            except Exception as e:
                logger.error(f"Failed to queue re-verification email after email change: {e}")
            messages.success(self.request, '✅ Профиль обновлён. Новый email нужно подтвердить — мы отправили письмо со ссылкой.')
        else:
            messages.success(self.request, '✅ Профиль обновлён')
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
        messages.success(self.request, '✅ Пароль изменён')
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = 'Изменить пароль — DOPX'
        return context


class PasswordResetViewCustom(PasswordResetView):
    """
    Django's PasswordResetForm молча "успешна" на несуществующий email (не
    палит, зарегистрирован ли адрес) — это же свойство делает эндпоинт
    удобной пушкой для флуда: без лимита можно было прогнать тысячи чужих
    email через форму за минуты и завалить исходящую почтовую очередь
    (Celery/notifications) чужими "инструкциями по сбросу".
    """
    template_name = 'auth/password_reset.html'
    email_template_name = 'emails/password_reset_email.txt'
    html_email_template_name = 'emails/password_reset_email.html'
    subject_template_name = 'emails/password_reset_subject.txt'
    success_url = reverse_lazy('users:password_reset_done')
    form_class = CustomPasswordResetForm

    def dispatch(self, request, *args, **kwargs):
        # Тот же паттерн, что в RegisterView.dispatch — лимит ДО валидации
        # формы.
        client_ip = get_client_ip(request)
        if request.method == 'POST' and client_ip:
            if is_rate_limited(
                f'password_reset:{client_ip}', PASSWORD_RESET_RATE_LIMIT, PASSWORD_RESET_RATE_LIMIT_WINDOW_SECONDS
            ):
                logger.warning(f"⚠️ Password reset rate limit exceeded for IP {client_ip}")
                messages.error(request, '⚠️ Слишком много попыток. Попробуйте позже.')
                return redirect('users:password_reset')
        return super().dispatch(request, *args, **kwargs)

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
        # Ключи берутся из User.DEFAULT_NOTIFICATION_SETTINGS, не хардкодом —
        # новое поле формы подхватывается без правки этого метода.
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
        # has_push_subscription питает Alpine-стейт кнопки в шаблоне
        # (notification_settings.html) — есть ли активная подписка хоть с
        # одного устройства пользователя, не только текущего браузера.
        push_subscriptions = self.request.user.push_subscriptions.order_by('-created_at')
        context['has_push_subscription'] = push_subscriptions.exists()
        # push_subscriptions — реальный список подписанных устройств для
        # карточки "Ваши устройства" (2026-08-31, по запросу пользователя:
        # раньше про "другие устройства" была только одна невнятная
        # строка текста, без возможности посмотреть, что именно подписано,
        # и отключить конкретное устройство удалённо).
        context['push_subscriptions'] = push_subscriptions
        return context


class UserLeaderboardView(ListView):
    model = User
    template_name = 'users/leaderboard.html'
    context_object_name = 'users'
    paginate_by = 20

    def get_queryset(self):
        # select_related('xp') — шаблон читает user.xp.level на каждой
        # строке (leaderboard.html), иначе N+1 на 20 пользователей страницы.
        qs = User.objects.filter(is_active=True, is_verified=True).select_related('xp').annotate(
            eval_count=Count('context_evaluations', distinct=True)
        ).filter(eval_count__gte=1).order_by('-trust_score', '-eval_count')
        # ?city= — локальный рейтинг "лучшие болельщики моего города".
        # Точное совпадение, не icontains: фильтр приходит из выпадающего
        # списка существующих значений city, не из свободного текста.
        city = self.request.GET.get('city', '').strip()
        if city:
            qs = qs.filter(city__iexact=city)
        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = 'Рейтинг пользователей — DOPX'
        context['selected_city'] = self.request.GET.get('city', '').strip()
        # Список городов для выпадающего фильтра — только те, что реально
        # встречаются у активных верифицированных пользователей (не пустой
        # справочник административных единиц Казахстана "на будущее").
        context['available_cities'] = (
            User.objects.filter(is_active=True, is_verified=True)
            .exclude(city='').values_list('city', flat=True).distinct().order_by('city')
        )
        return context


class PlayerLeaderboardView(ListView):
    template_name = 'players/leaderboard.html'
    context_object_name = 'players'
    paginate_by = 20

    def get_queryset(self):
        from players.models import Player
        from aggregates.models import PlayerMatchAggregate
        from django.db.models import Avg, Count, Sum, Q
        qs = Player.objects.filter(is_active=True).annotate(
            avg_performance=Avg('match_aggregates__performance_score'),
            total_matches=Count('match_aggregates', distinct=True),
            total_votes=Sum('match_aggregates__total_votes')
        ).filter(
            avg_performance__isnull=False,
            total_matches__gte=1
        ).order_by('-avg_performance')
        # ?league= — рейтинг игроков в разрезе лиги. У Player/Team нет
        # прямого FK на League (лига — атрибут матча, не команды), поэтому
        # фильтруем через match_aggregates__match__league, не team__league.
        # distinct() — игрок может встретиться в лиге по нескольким матчам,
        # без него JOIN размножил бы строки и annotate() выше считал бы неверно.
        league_id = self.request.GET.get('league', '').strip()
        if league_id:
            qs = qs.filter(match_aggregates__match__league_id=league_id).distinct()
        return qs

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
    """
    Продуктовый аудит, раздел 5b ("Follow-граф"): тап на кнопку "Подписаться"
    на странице игрока/команды. Один эндпоинт на оба типа целей (а не
    `toggle_player_follow`/`toggle_team_follow` дублирующимися вьюхами) —
    логика идентична, различается только модель, на которую смотрим.
    Возвращает HTMX-партиал с новым состоянием кнопки (та же схема, что
    `events:react` — мгновенный свап без перезагрузки страницы).

    Rate-limit по user.id (30/мин) — без него скрипт мог бы задиспэтчить
    Follow.objects.create/delete по кругу без ограничений (дешёвый способ
    засорить follow-граф и очередь персонализированных уведомлений,
    users/tasks.py). 429 без тела — HTMX по умолчанию не свапает контент
    вне 2xx, кнопка просто не обновится вместо падения страницы.
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
    """
    Продуктовый аудит, раздел 5c ("PWA + Web Push"): сохраняет подписку,
    присланную `static/js/push.js::dopxSubscribePush` (JSON-тело —
    результат `PushSubscription.toJSON()` из Push API браузера).
    `update_or_create` по `endpoint` — повторная подписка с того же
    браузера (например, после очистки локальной БД воркера) обновляет
    ключи, а не падает на UniqueConstraint.
    """
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

    PushSubscription.objects.update_or_create(
        endpoint=endpoint,
        defaults={
            'user': request.user,
            'p256dh': p256dh,
            'auth': auth,
            'user_agent': request.META.get('HTTP_USER_AGENT', '')[:255],
        },
    )
    return JsonResponse({'ok': True})


@require_POST
@login_required
def push_unsubscribe(request):
    """Удаляет подписку по endpoint (см. dopxUnsubscribePush)."""
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
    """
    Отключить КОНКРЕТНОЕ устройство по id записи PushSubscription — в
    отличие от push_unsubscribe (который работает только для ТЕКУЩЕГО
    браузера, через его собственный PushManager.getSubscription()), эта
    вьюха вызывается из обычной POST-формы на странице настроек и удаляет
    запись без участия Push API браузера. Это осознанно: пользователь
    должен иметь возможность отключить старый/чужой/потерянный телефон,
    сидя за ноутбуком — без этого единственный способ снять подписку с
    устройства был "открыть настройки именно на нём" (2026-08-31, по
    запросу пользователя после того, как обнаружил забытую подписку
    Chrome, зайдя с Safari на том же компьютере).

    Со стороны браузера, чья подписка отозвана так — "тихо" продолжает
    считать себя подписанным (localStorage/Push API не в курсе), пока
    push реально не придёт: send_push_to_user (notifications/services.py)
    получит 404/410 от push-сервиса на несуществующий endpoint и удалит
    "осиротевшую" запись сам (см. её докстринг) — но т.к. записи уже нет,
    это просто no-op. Разряженный edge-case (тот браузер решит, что он
    "включён", хотя реально push до него больше не дойдёт), приемлем ради
    простоты — то же самое происходит и у любого сервиса с "выйти со всех
    устройств".
    """
    from users.models import PushSubscription

    deleted, _ = PushSubscription.objects.filter(user=request.user, id=subscription_id).delete()
    if deleted:
        messages.success(request, '✅ Устройство отключено от push-уведомлений')
    return redirect('users:notification_settings')