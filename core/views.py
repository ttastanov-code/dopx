# core/views.py
"""
standings_preview читает уже готовый TeamSeasonStats (считает
aggregates/tasks.py::recalculate_season_standings по расписанию Celery Beat),
а не пересчитывает таблицу заново на каждый промах кэша — иначе формула
очков могла бы разъехаться между двумя местами.
IP клиента — через core.utils.get_client_ip, единая реализация на проект.
"""
import logging
import os
from datetime import timedelta
from django.conf import settings
from django.contrib import messages
from django.db.models import Count, Avg, F, Q, Sum
from django.core.cache import cache
from django.http import HttpResponse
from django.shortcuts import redirect, render, get_object_or_404
from django.template.loader import render_to_string
from django.utils import timezone
from django.views.generic import TemplateView, View
from django.core.mail import send_mail, EmailMultiAlternatives
from django.core.files.storage import default_storage

from aggregates.models import MatchAggregate, PlayerMatchAggregate
from core.nominations import MIN_VOTES as NOMINATION_MIN_VOTES, get_nominations
from core.utils import get_client_ip
from evaluations.models import ContextEvaluation, EvaluationSession, MatchEvaluation, PlayerEvaluation, TeamEvaluation
from matches.models import Match
from seasons.models import Season
from teams.models import Team, TeamSeasonStats
from users.models import SuspiciousActivityFlag, User

from notifications.models import ContactSubmission

logger = logging.getLogger(__name__)


class HomeView(TemplateView):
    """Главная страница — дашборд"""
    template_name = 'core/home.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        now = timezone.now()

        # === Существующие данные ===
        recent_matches = Match.objects.filter(
            status='finished', start_time__lte=now
        ).select_related('home_team', 'away_team', 'league', 'season'
        ).prefetch_related('aggregate').order_by('-start_time')[:6]

        upcoming_matches = Match.objects.filter(
            status='scheduled', start_time__gte=now
        ).select_related('home_team', 'away_team', 'league', 'season'
        ).order_by('start_time')[:4]

        top_players = PlayerMatchAggregate.objects.select_related(
            'player', 'player__team'
        ).order_by('-performance_score')[:5]

        # === НОВАЯ СТАТИСТИКА ===
        total_evals = (
            MatchEvaluation.objects.count() +
            TeamEvaluation.objects.count() +
            PlayerEvaluation.objects.count()
        )

        active_users = User.objects.filter(
            context_evaluations__created_at__gte=now - timedelta(days=7)
        ).distinct().count()

        # Только матчи с реальными голосами — иначе Avg=None превращается в
        # "0,0" на главной, что читается как низкий балл, а не как "нет данных".
        match_aggs_with_votes = MatchAggregate.objects.filter(total_votes__gt=0)
        avg_entertainment = match_aggs_with_votes.aggregate(
            avg=Avg('avg_entertainment')
        )['avg']
        avg_drama = match_aggs_with_votes.aggregate(
            avg=Avg('drama_index')
        )['avg']

        metrics = {
            'avg_entertainment': round(avg_entertainment, 1) if avg_entertainment is not None else None,
            'avg_drama': round(avg_drama, 0) if avg_drama is not None else None,
        }

        stats = {
            'total_matches': Match.objects.count(),
            'active_voting': Match.objects.filter(
                voting_open_until__gte=now, status='finished'
            ).count(),
            'total_evaluations': total_evals,
            'active_users': active_users,
        }

        # === ТОП КОМАНД ПО ОЦЕНКАМ ===
        top_teams = Team.objects.annotate(
            avg_rating=Avg(
                (F('team_evaluations__tactics') +
                 F('team_evaluations__effort') +
                 F('team_evaluations__organization') +
                 F('team_evaluations__mentality')) / 4.0
            )
        ).filter(
            avg_rating__isnull=False,
            is_active=True
        ).order_by('-avg_rating')[:5]

        # === Активная сессия пользователя ===
        # Фильтр по voting_open_until — незавершённая сессия по уже
        # закрывшемуся голосованию не должна предлагаться к продолжению
        # (тот же паттерн в profile/dashboard::ProfileView).
        active_match_id = None
        if self.request.user.is_authenticated:
            active_session = EvaluationSession.objects.filter(
                user=self.request.user,
                status__in=['started', 'in_progress'],
                match__voting_open_until__gte=now,
                match__status='finished',
            ).select_related('match').first()
            if active_session:
                active_match_id = active_session.match.id

        # === НОМИНАЦИИ СЕЗОНА ===
        # Витрина "интересных фактов" по всей платформе (без фильтра по
        # лиге/сезону) — см. core/nominations.py за полным объяснением
        # идеи и статистической защиты (MIN_VOTES).
        nominations = get_nominations()

        context.update({
            'recent_matches': recent_matches,
            'upcoming_matches': upcoming_matches,
            'top_players': top_players,
            'top_teams': top_teams,
            'stats': stats,
            'metrics': metrics,
            'active_match_id': active_match_id,
            'nominations': nominations,
            'nomination_min_votes': NOMINATION_MIN_VOTES,
            'page_title': 'DOPX — Голос трибун измеряем',
            'now': now,
        })
        return context


def standings_preview(request):
    """HTMX partial превью турнирной таблицы — читает готовую TeamSeasonStats, не пересчитывает на лету."""
    season = Season.objects.filter(is_active=True).first()
    if not season:
        return HttpResponse('''
        <div class="text-center py-8 opacity-60">
            <i class="ti ti-trophy-off text-3xl mb-2"></i>
            <p class="text-sm">Нет активного сезона</p>
        </div>
        ''')

    cache_key = f'league_{season.league.id}_season_{season.id}_standings_preview'
    cached_html = cache.get(cache_key)

    if cached_html:
        return HttpResponse(cached_html)

    stats_rows = (
        TeamSeasonStats.objects.filter(season=season)
        .select_related('team')
        .order_by('position', '-points', '-goal_diff', '-goals_scored')[:10]
    )

    standings_list = [
        {
            'team_id': str(row.team_id),
            'team_name': row.team.name,
            'team_logo_url': row.team.logo_url,
            'played': row.played,
            'wins': row.wins,
            'draws': row.draws,
            'losses': row.losses,
            'goals_scored': row.goals_scored,
            'goals_conceded': row.goals_conceded,
            'goal_diff': row.goal_diff,
            'points': row.points,
        }
        for row in stats_rows
    ]

    html = render_to_string('components/_standings_preview.html', {
        'standings': standings_list,
        'season': season,
    })

    cache.set(cache_key, html, 300)

    return HttpResponse(html)


class RulesView(TemplateView):
    """Страница правил платформы. XP-таблица и бейджи приходят из кода (evaluations/views.py, users/badges.py::BADGE_CATALOG), не захардкожены в шаблоне."""
    template_name = 'core/rules.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = 'Правила платформы — DOPX'

        from evaluations.views import (
            XP_CONTEXT_STEP, XP_TEAMS_STEP, XP_PLAYERS_STEP_MAX,
            XP_COACHES_STEP, XP_REFEREE_STEP, XP_FINAL_STEP,
        )
        from users.badges import BADGE_CATALOG
        from users.models import LEVEL_XP_BASE, cumulative_xp_for_level

        context['xp_steps'] = {
            'context': XP_CONTEXT_STEP,
            'teams': XP_TEAMS_STEP,
            'players': XP_PLAYERS_STEP_MAX,
            'coaches': XP_COACHES_STEP,
            'referee': XP_REFEREE_STEP,
            'final': XP_FINAL_STEP,
        }
        context['xp_full_match_total'] = (
            XP_CONTEXT_STEP + XP_TEAMS_STEP + XP_PLAYERS_STEP_MAX
            + XP_COACHES_STEP + XP_REFEREE_STEP + XP_FINAL_STEP
        )
        # Пара примеров кумулятивного порога уровня — для наглядной иллюстрации
        # растущего шага кривой `LEVEL_XP_BASE * N * (N-1)`, вместо словесного
        # описания формулы.
        context['level_examples'] = [
            {'level': n, 'xp': cumulative_xp_for_level(n)} for n in (2, 3, 4, 5, 10)
        ]

        badge_categories = [
            ('engagement', 'Вовлечённость', 'ti-flame', [
                'first_evaluation', 'active_fan_10', 'active_fan_50', 'active_fan_150',
                'streak_7', 'streak_30', 'streak_100',
            ]),
            ('quality', 'Качество и точность', 'ti-target-arrow', [
                'accurate_analyst', 'foresight', 'bias_free', 'early_bird',
                'judge_of_judges', 'polyglot',
            ]),
            ('status', 'Дерби и статусные', 'ti-crown', [
                'derby_hunter', 'monthly_champion',
            ]),
            ('secret', 'Секретные', 'ti-lock-question', [
                'founder',
            ]),
        ]
        context['badge_categories'] = [
            {
                'key': key,
                'title': title,
                'icon': icon,
                'badges': [BADGE_CATALOG[code] for code in codes if code in BADGE_CATALOG],
            }
            for key, title, icon, codes in badge_categories
        ]
        context['badge_total_count'] = len(BADGE_CATALOG)
        context['nomination_min_votes'] = NOMINATION_MIN_VOTES
        return context


class ContactsView(TemplateView):
    """Страница обратной связи"""
    template_name = 'core/contacts.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = 'Контакты — DOPX'
        now = timezone.now()
        context['stats'] = {
            'total_matches': Match.objects.count(),
            'total_evaluations': (
                MatchEvaluation.objects.count() +
                PlayerEvaluation.objects.count() +
                TeamEvaluation.objects.count()
            ),
            'active_users': User.objects.filter(
                context_evaluations__created_at__gte=now - timedelta(days=7)
            ).distinct().count(),
            'avg_drama': MatchAggregate.objects.aggregate(
                avg=Avg('drama_index')
            )['avg'] or 0,
        }
        return context

    def post(self, request, *args, **kwargs):
        """Обработка формы обратной связи"""
        category = request.POST.get('category', 'general')
        email = request.POST.get('email', '').strip()
        subject = request.POST.get('subject', 'Обращение через сайт').strip()
        message = request.POST.get('message', '').strip()
        screenshot = request.FILES.get('screenshot')

        # Валидация
        if len(message) < 20:
            messages.error(request, 'Сообщение слишком короткое (мин. 20 символов)')
            return redirect('core:contacts')

        user = request.user if request.user.is_authenticated else None

        if not user and not email:
            messages.error(request, 'Укажите email для связи')
            return redirect('core:contacts')

        # Проверка размера файла (макс. 5MB)
        if screenshot and screenshot.size > 5 * 1024 * 1024:
            messages.error(request, 'Файл слишком большой (макс. 5MB)')
            return redirect('core:contacts')

        try:
            # Создаём обращение
            submission = ContactSubmission.objects.create(
                user=user,
                guest_email=email if not user else '',
                category=category,
                subject=subject,
                message=message,
                ip_address=get_client_ip(request),
                user_agent=request.META.get('HTTP_USER_AGENT', '')[:500],
            )

            # ✅ СОХРАНЯЕМ ФАЙЛ
            if screenshot:
                submission.attachment.save(
                    screenshot.name,
                    screenshot,
                    save=True
                )
                logger.info(f"✅ Файл сохранён: {submission.attachment.name}")

            # Отправка email админу
            self.send_admin_notification(submission)

            # Уведомление пользователя
            if user and user.is_verified:
                self.send_user_confirmation(submission)

            messages.success(request, '✅ Сообщение отправлено! Мы ответим в течение 24 часов.')
            logger.info(
                f"Contact submission #{submission.id} from {submission.contact_email} "
                f"(category: {category}, has_attachment: {bool(submission.attachment)})"
            )

        except Exception as e:
            logger.error(f"Contact form error: {type(e).__name__}: {e}", exc_info=True)
            messages.error(
                request,
                f'❌ Ошибка отправки: {str(e)}. Напишите на support@dopx.kz'
            )
            return redirect('core:contacts')

        return redirect('core:contacts')

    def send_admin_notification(self, submission):
        """Отправка уведомления админу"""
        admin_email = getattr(settings, 'CONTACT_EMAIL', 'admin@dopx.kz')
        from_email = getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@dopx.kz')
        site_url = getattr(settings, 'SITE_URL', 'https://dopx.kz')

        # "Право на ответ" — юридически значимая категория, письмо должно
        # выделяться в почте founder'а среди обычных багрепортов.
        urgency_prefix = "🚨 ПРАВО НА ОТВЕТ" if submission.category == 'dispute' else "📬 Новое обращение"
        subject = f"{urgency_prefix} #{str(submission.id)[:8]} ({submission.get_category_display()})"

        html_message = render_to_string('emails/contact_form.html', {
            'submission': submission,
            'category': submission.get_category_display(),
            'email': submission.contact_email,
            'username': submission.user.username if submission.user else 'Гость',
            'message': submission.message,
            'has_attachment': bool(submission.attachment),
            'site_name': 'DOPX',
            'site_url': site_url,
        })

        email = EmailMultiAlternatives(
            subject=subject,
            body='',
            from_email=from_email,
            to=[admin_email],
        )
        email.attach_alternative(html_message, "text/html")

        # Прикрепляем файл к письму
        if submission.attachment:
            try:
                submission.attachment.open('rb')
                email.attach(
                    os.path.basename(submission.attachment.name),
                    submission.attachment.read(),
                    submission.attachment.content_type or 'application/octet-stream'
                )
                submission.attachment.close()
                logger.info(f"Attached file to admin email: {submission.attachment.name}")
            except Exception as e:
                logger.error(f"Failed to attach file to admin email: {e}")

        email.send(fail_silently=False)
        logger.info(f"✅ Admin notification sent to {admin_email}")

    def send_user_confirmation(self, submission):
        """Подтверждение пользователю"""
        if not submission.user or not submission.user.email:
            return

        from_email = getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@dopx.kz')
        site_url = getattr(settings, 'SITE_URL', 'https://dopx.kz')

        subject = f"✅ Ваше обращение #{str(submission.id)[:8]} принято"

        html_message = render_to_string('emails/contact_confirmation.html', {
            'submission': submission,
            'username': submission.user.username,
            'site_name': 'DOPX',
            'site_url': site_url,
        })

        send_mail(
            subject=subject,
            message='',
            from_email=from_email,
            recipient_list=[submission.user.email],
            html_message=html_message,
            fail_silently=False,
        )


class ContactSubmissionDetailView(View):
    """Просмотр обращения (для пользователя)"""
    def get(self, request, pk):
        submission = get_object_or_404(
            ContactSubmission,
            pk=pk,
            user=request.user if request.user.is_authenticated else None
        )
        return render(request, 'core/contact_detail.html', {
            'submission': submission,
            'page_title': f'Обращение #{str(submission.id)[:8]}',
        })


class PrivacyPolicyView(TemplateView):
    """Страница Политики конфиденциальности"""
    template_name = 'core/privacy.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = 'Политика конфиденциальности — DOPX'
        return context


def robots_txt(request):
    """
    Отдельная FBV, без лишнего TemplateView ради 8 строк текста.
    Явно закрываем то же самое, что уже не публично в UI (admin/api/
    личный кабинет/DjDT) — от индексации это тоже должно быть спрятано,
    иначе Google с радостью проиндексирует страницу входа в чужой профиль.
    """
    lines = [
        "User-agent: *", "Allow: /",
        "Disallow: /admin/", "Disallow: /api/", "Disallow: /users/profile/", "Disallow: /__debug__/",
        "", f"Sitemap: {settings.SITE_URL}/sitemap.xml",
    ]
    return HttpResponse("\n".join(lines), content_type="text/plain")


def service_worker(request):
    """
    Продуктовый аудит, раздел 5c ("PWA + Web Push"): отдаёт `static/sw.js`
    с URL `/sw.js` (КОРЕНЬ сайта), а не `/static/sw.js`. Это НЕ косметика —
    scope service worker'а по умолчанию равен директории, из которой он
    был запрошен браузером: зарегистрированный с `/static/sw.js` контролировал
    бы только `/static/*` и никогда не увидел бы навигацию по обычным
    страницам сайта, а `manifest.json.start_url: "/"` требует, чтобы
    именно `/` попадал под scope зарегистрированного воркера — иначе
    браузер не посчитает сайт "installable" (условие PWA-манифеста).
    Заголовок `Service-Worker-Allowed: /` — явное подтверждение того же
    для браузеров, которые проверяют его строже, чем просто путь запроса.
    """
    sw_path = settings.BASE_DIR / 'static' / 'sw.js'
    try:
        with open(sw_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except FileNotFoundError:
        return HttpResponse('', content_type='application/javascript', status=404)

    response = HttpResponse(content, content_type='application/javascript')
    response['Service-Worker-Allowed'] = '/'
    response['Cache-Control'] = 'no-cache'
    return response


class AntiFraudView(TemplateView):
    """
    Публичная страница "Как мы боремся с накруткой" — продаёт trust_score/
    SuspiciousActivityFlag как реальное отличие от "ещё одного форума
    фанатов", а не маркетинговую фразу без данных. Живые цифры конвертят
    скептиков лучше общих слов о честности.
    """
    template_name = "core/anti_fraud.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "Как мы боремся с накруткой — DOPX"
        context["meta_description"] = (
            "Методология DOPX: взвешенное голосование по Trust Score, "
            "анти-фрод очередь модерации, защита от накрутки оценок."
        )
        # Кэш 1ч: публичная страница, свежесть до часа более чем достаточна,
        # не считаем агрегаты при каждом заходе бота/пользователя.
        context["stats"] = cache.get_or_set("anti_fraud_public_stats", self._compute_stats, timeout=3600)
        return context

    @staticmethod
    def _compute_stats() -> dict:
        total_flags = SuspiciousActivityFlag.objects.count()
        total_evaluations = ContextEvaluation.objects.count()
        by_status = dict(
            SuspiciousActivityFlag.objects.values_list("status").annotate(count=Count("id")).order_by()
        )
        return {
            "total_flags": total_flags,
            "total_evaluations": total_evaluations,
            "flag_rate_percent": round(total_flags / total_evaluations * 100, 2) if total_evaluations else 0.0,
            "pending_count": by_status.get("pending", 0),
            "confirmed_count": by_status.get("confirmed", 0),
            "dismissed_count": by_status.get("dismissed", 0),
        }


class MatchShareCardView(View):
    """/share/match/<uuid:match_id>/card.png — редирект на закэшированную
    карточку. Используется и как og:image страницы матча, и как прямая
    ссылка при шеринге в Telegram/WhatsApp."""

    def get(self, request, match_id):
        from core.services.share_cards import build_match_share_card

        match = get_object_or_404(Match.objects.select_related("home_team", "away_team"), pk=match_id)
        top = (
            PlayerMatchAggregate.objects.filter(match=match)
            .select_related("player").order_by("-performance_score").first()
        )
        path = build_match_share_card(
            home_team=match.home_team.name, away_team=match.away_team.name,
            home_score=match.home_score or 0, away_score=match.away_score or 0,
            top_player_name=f"{top.player.first_name} {top.player.last_name}" if top else "—",
            top_player_score=top.performance_score if top else 0.0,
        )
        return redirect(default_storage.url(path))


def handler_404(request, exception):
    return render(request, 'errors/404.html', status=404)


def handler_500(request):
    return render(request, 'errors/500.html', status=500)