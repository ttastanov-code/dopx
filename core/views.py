# core/views.py
"""Общие страницы: главная, правила, контакты, политика, антифрод, share-карточки, виджеты."""
import logging
import os
from datetime import timedelta
from django.conf import settings
from django.contrib import messages
from django.db.models import Count, Avg, F, Q, Sum
from django.core.cache import cache
from django.http import HttpResponse, Http404
from django.shortcuts import redirect, render, get_object_or_404
from django.urls import reverse
from django.template.loader import render_to_string
from django.utils import timezone
from django.views.decorators.clickjacking import xframe_options_exempt
from django.views.generic import TemplateView, View
from django.core.mail import send_mail, EmailMultiAlternatives
from django.utils.html import strip_tags
from django.core.files.storage import default_storage

from aggregates.models import MatchAggregate, PlayerMatchAggregate
from aggregates.services import MIN_VOTES_FOR_DISPLAY
from core.forms import ContactAntiBotForm
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
    """Главная страница."""
    template_name = 'core/home.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        now = timezone.now()

        recent_matches = list(Match.objects.filter(
            status='finished', start_time__lte=now
        ).select_related('home_team', 'away_team', 'league', 'season'
        ).prefetch_related('aggregate', 'home_team__rivals').order_by('-start_time')[:6])

        upcoming_matches = list(Match.objects.filter(
            status='scheduled', start_time__gte=now
        ).select_related('home_team', 'away_team', 'league', 'season'
        ).prefetch_related('home_team__rivals').order_by('start_time')[:4])

        # Live-матчи для главной.
        live_matches = list(Match.objects.filter(
            status='live'
        ).select_related('home_team', 'away_team', 'league', 'season'
        ).prefetch_related('home_team__rivals').order_by('start_time'))

        # Данные карточек для всех трёх списков — одним вызовом attach_card_extras.
        from matches.card_services import attach_card_extras

        attach_card_extras(recent_matches + upcoming_matches + live_matches, self.request)

        # Только игроки с достаточным числом голосов.
        top_players = PlayerMatchAggregate.objects.select_related(
            'player', 'player__team'
        ).filter(total_votes__gte=MIN_VOTES_FOR_DISPLAY).order_by('-performance_score')[:5]

        total_evals = (
            MatchEvaluation.objects.count() +
            TeamEvaluation.objects.count() +
            PlayerEvaluation.objects.count()
        )

        active_users = User.objects.filter(
            context_evaluations__created_at__gte=now - timedelta(days=7)
        ).distinct().count()

        # Только матчи с голосами, иначе Avg=None превратится в «0,0».
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

        # Топ команд по оценкам.
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

        # Незавершённая сессия пользователя — только если голосование ещё открыто.
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

        # Номинации сезона (core/nominations.py).
        nominations = get_nominations()

        # Embed-код турнирной таблицы.
        widget_url = self.request.build_absolute_uri(reverse('core:standings_widget'))
        standings_widget_embed_code = (
            f'<iframe src="{widget_url}" width="340" height="360" '
            f'style="border:none;border-radius:12px;overflow:hidden" '
            f'title="Турнирная таблица КПЛ на DOPX"></iframe>'
        )

        context.update({
            'recent_matches': recent_matches,
            'upcoming_matches': upcoming_matches,
            'live_matches': live_matches,
            'top_players': top_players,
            'top_teams': top_teams,
            'stats': stats,
            'metrics': metrics,
            'active_match_id': active_match_id,
            'nominations': nominations,
            'nomination_min_votes': NOMINATION_MIN_VOTES,
            'standings_widget_embed_code': standings_widget_embed_code,
            'page_title': 'DOPX — Голос трибун измеряем',
            'now': now,
        })
        return context


def standings_preview(request):
    """HTMX-превью турнирной таблицы из готовой TeamSeasonStats."""
    # Активный сезон основной лиги (Season.get_primary_active).
    season = Season.get_primary_active()
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


@xframe_options_exempt
def standings_widget(request):
    """Embed-виджет турнирной таблицы для чужого <iframe>."""
    season = Season.get_primary_active()
    standings_list = []
    if season:
        stats_rows = (
            TeamSeasonStats.objects.filter(season=season)
            .select_related('team')
            .order_by('position', '-points', '-goal_diff', '-goals_scored')[:10]
        )
        standings_list = [
            {
                'team_name': row.team.name,
                'team_logo_url': row.team.logo_url,
                'played': row.played,
                'points': row.points,
            }
            for row in stats_rows
        ]

    from partners.services import track_widget_embed_view

    track_widget_embed_view(
        widget_type="standings",
        entity_id=str(season.league_id) if season else "none",
        request=request,
    )

    return render(request, 'widgets/standings.html', {
        'season': season,
        'standings': standings_list,
    })


class RulesView(TemplateView):
    """Правила платформы. XP и бейджи берутся из кода, не из шаблона."""
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
        # Примеры порогов уровня для иллюстрации кривой XP.
        context['level_examples'] = [
            {'level': n, 'xp': cumulative_xp_for_level(n)} for n in (2, 3, 4, 5, 10)
        ]

        badge_categories = [
            ('engagement', 'Вовлечённость', 'ti-flame', [
                'first_evaluation', 'active_fan_10', 'active_fan_50', 'active_fan_150',
                'streak_7', 'streak_30', 'streak_100',
                'full_season',
            ]),
            ('quality', 'Качество и точность', 'ti-target-arrow', [
                'accurate_analyst', 'foresight', 'bias_free', 'early_bird',
                'judge_of_judges', 'polyglot',
                'coach_expert', 'both_sides',
            ]),
            # Прогнозные достижения — отдельной категорией.
            ('predictions', 'Прогнозы', 'ti-chart-line', [
                'first_prediction', 'prediction_streak_7', 'prediction_streak_30', 'prediction_streak_100',
                'stable_hand', 'derby_prophet', 'against_the_tide',
            ]),
            ('status', 'Дерби и статусные', 'ti-crown', [
                'derby_hunter', 'monthly_champion',
            ]),
            ('secret', 'Секретные', 'ti-lock-question', [
                'founder',
            ]),
            # Легендарные — отдельная категория с особым визуалом.
            ('legendary', 'Легендарные', 'ti-diamond', [
                'perfect_tour', 'streak_250', 'prediction_streak_200',
                'season_completionist', 'max_trust',
            ]),
        ]
        # Страница публичная — имена секретных бейджей маскируем всегда.
        def _safe_badge(code: str) -> dict:
            d = BADGE_CATALOG[code]
            return {
                'code': code,
                'name': '???' if d.is_secret else d.name,
                'rarity': d.rarity,
                'is_secret': d.is_secret,
            }

        context['badge_categories'] = [
            {
                'key': key,
                'title': title,
                'icon': icon,
                'badges': [_safe_badge(code) for code in codes if code in BADGE_CATALOG],
            }
            for key, title, icon, codes in badge_categories
        ]
        context['badge_total_count'] = len(BADGE_CATALOG)
        context['nomination_min_votes'] = NOMINATION_MIN_VOTES
        return context


class ContactsView(TemplateView):
    """Страница обратной связи."""
    template_name = 'core/contacts.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = 'Контакты — DOPX'
        # Новая форма на каждый рендер — свежие time-trap и капча.
        context['antibot_form'] = ContactAntiBotForm()

        # ?category=data_error&match=<uuid> — жалоба на данные матча.
        # Битый UUID просто игнорируем.
        context['related_match'] = None
        match_id = self.request.GET.get('match', '').strip()
        if match_id:
            try:
                import uuid as uuid_module
                uuid_module.UUID(match_id)
            except (ValueError, TypeError, AttributeError):
                pass
            else:
                context['related_match'] = Match.objects.filter(id=match_id).select_related(
                    'home_team', 'away_team'
                ).first()

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
        """Обработка формы обратной связи."""
        # Анти-бот (honeypot + time-trap + капча) — первым делом.
        antibot_form = ContactAntiBotForm(request.POST)
        if not antibot_form.is_valid():
            if 'captcha' in antibot_form.errors:
                messages.error(request, 'Неверный текст с картинки. Попробуйте ещё раз.')
            else:
                # Боту подробности не нужны — отвечаем как при неверной капче.
                messages.error(request, 'Не удалось обработать форму. Попробуйте ещё раз.')
            return redirect('core:contacts')

        category = request.POST.get('category', 'general')
        email = request.POST.get('email', '').strip()
        subject = request.POST.get('subject', 'Обращение через сайт').strip()
        message = request.POST.get('message', '').strip()
        screenshot = request.FILES.get('screenshot')

        # Скрытое поле с id матча. Невалидный/несуществующий id игнорируем.
        related_match = None
        related_match_id = request.POST.get('related_match', '').strip()
        if related_match_id:
            try:
                import uuid as uuid_module
                uuid_module.UUID(related_match_id)
            except (ValueError, TypeError, AttributeError):
                pass
            else:
                related_match = Match.objects.filter(id=related_match_id).first()

        # Валидация
        if len(message) < 20:
            messages.error(request, 'Слишком короткое сообщение. Минимум 20 символов.')
            return redirect('core:contacts')

        user = request.user if request.user.is_authenticated else None

        if not user and not email:
            messages.error(request, 'Укажите email для связи.')
            return redirect('core:contacts')

        # Файл не больше 5 МБ
        if screenshot and screenshot.size > 5 * 1024 * 1024:
            messages.error(request, 'Файл слишком большой. Максимум 5 МБ.')
            return redirect('core:contacts')

        try:
            # Создаём обращение
            submission = ContactSubmission.objects.create(
                user=user,
                guest_email=email if not user else '',
                category=category,
                subject=subject,
                message=message,
                related_match=related_match,
                ip_address=get_client_ip(request),
                user_agent=request.META.get('HTTP_USER_AGENT', '')[:500],
            )

            # Сохраняем файл
            if screenshot:
                submission.attachment.save(
                    screenshot.name,
                    screenshot,
                    save=True
                )
                logger.info(f"✅ Файл сохранён: {submission.attachment.name}")

            # Письмо админу
            self.send_admin_notification(submission)

            # Подтверждение получения — всем, включая гостей.
            self.send_user_confirmation(submission)

            messages.success(request, 'Сообщение отправлено. Мы ответим вам на почту.')
            logger.info(
                f"Contact submission #{submission.id} from {submission.contact_email} "
                f"(category: {category}, has_attachment: {bool(submission.attachment)})"
            )

        except Exception as e:
            # Пользователю — общий текст, детали в логе.
            logger.error(f"Contact form error: {type(e).__name__}: {e}", exc_info=True)
            messages.error(request, 'Не удалось отправить сообщение. Напишите нам на support@dopx.kz.')
            return redirect('core:contacts')

        return redirect('core:contacts')

    def send_admin_notification(self, submission):
        """Письмо админу об обращении."""
        admin_email = getattr(settings, 'CONTACT_EMAIL', 'admin@dopx.kz')
        from_email = getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@dopx.kz')
        site_url = getattr(settings, 'SITE_URL', 'https://dopx.kz')

        # «Право на ответ» — выделяем в теме письма.
        urgency_prefix = "ПРАВО НА ОТВЕТ" if submission.category == 'dispute' else "Новое обращение"
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
            # text/plain из html — пустая текстовая часть выглядит как спам.
            subject=subject,
            body=strip_tags(html_message),
            from_email=from_email,
            to=[admin_email],
        )
        email.attach_alternative(html_message, "text/html")

        # Прикрепляем файл
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
        """Подтверждение получения на submission.contact_email (пользователь или гость)."""
        recipient = submission.contact_email
        if not recipient:
            return

        from_email = getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@dopx.kz')
        site_url = getattr(settings, 'SITE_URL', 'https://dopx.kz')

        subject = f"Ваше обращение #{str(submission.id)[:8]} принято"

        html_message = render_to_string('emails/contact_confirmation.html', {
            'submission': submission,
            'username': submission.user.username if submission.user else 'Гость',
            'site_name': 'DOPX',
            'site_url': site_url,
        })

        send_mail(
            subject=subject,
            message=strip_tags(html_message),
            from_email=from_email,
            recipient_list=[recipient],
            html_message=html_message,
            fail_silently=False,
        )


class ContactSubmissionDetailView(View):
    """Просмотр обращения пользователем."""
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
    """Политика конфиденциальности."""
    template_name = 'core/privacy.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = 'Политика конфиденциальности — DOPX'
        return context


def robots_txt(request):
    """robots.txt: закрываем admin/api/кабинет от индексации."""
    lines = [
        "User-agent: *", "Allow: /",
        "Disallow: /admin/", "Disallow: /api/", "Disallow: /users/profile/", "Disallow: /__debug__/",
        "", f"Sitemap: {settings.SITE_URL}/sitemap.xml",
    ]
    return HttpResponse("\n".join(lines), content_type="text/plain")


def service_worker(request):
    """Отдаёт static/sw.js по /sw.js — чтобы scope service worker'а был весь сайт."""
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
    """Публичная страница «Как мы боремся с накруткой» с живыми цифрами."""
    template_name = "core/anti_fraud.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "Как мы боремся с накруткой — DOPX"
        context["meta_description"] = (
            "Методология DOPX: взвешенное голосование по Trust Score, "
            "анти-фрод очередь модерации, защита от накрутки оценок."
        )
        # Кэш на час.
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
    """/share/match/<id>/card.png — редирект на закэшированную карточку матча (og:image, шеринг)."""

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


class MatchDNAShareCardView(View):
    """/share/match/<id>/dna-card.png — карточка «ДНК матча».
    404, если по матчу нет голосов.
    """

    def get(self, request, match_id):
        from django.db.models import Count
        from evaluations.models import ContextEvaluation, MatchEvaluation
        from matches.services import build_match_dna
        from core.services.share_cards import build_match_dna_share_card

        match = get_object_or_404(
            Match.objects.select_related("home_team", "away_team"), pk=match_id
        )
        match_agg = getattr(match, "aggregate", None)
        if match_agg is None or match_agg.total_votes == 0:
            raise Http404("Нет голосов по этому матчу")

        events = list(match.events.select_related("player").order_by("minute")[:20])
        referee_agg = match.referee_aggregates.first()
        # Тот же порог голосов, что на странице матча.
        top_players = list(
            PlayerMatchAggregate.objects.filter(match=match, total_votes__gte=MIN_VOTES_FOR_DISPLAY)
            .select_related("player").order_by("-performance_score")[:1]
        )
        worst_players = list(
            PlayerMatchAggregate.objects.filter(match=match, total_votes__gte=MIN_VOTES_FOR_DISPLAY)
            .select_related("player").order_by("performance_score")[:1]
        )
        fan_support = list(ContextEvaluation.objects.filter(
            match=match
        ).exclude(
            supported_team__isnull=True
        ).values(
            "supported_team__id", "supported_team__name"
        ).annotate(count=Count("id")).order_by("-count")[:2])
        match_evaluations = list(
            MatchEvaluation.objects.filter(match=match).only("entertainment", "tension", "fairness")
        )
        match_dna = build_match_dna(
            match, match_agg, events, referee_agg,
            match_evaluations=match_evaluations, top_players=top_players,
            worst_players=worst_players, fan_support=fan_support,
        )
        if match_dna is None:
            raise Http404("Нет голосов по этому матчу")

        # Приоритет «самого заметного факта» — как порядок блоков на странице матча.
        headline = (
            match_dna["controversial_episode"]
            or match_dna["referee_divergence"]
            or match_dna["turning_point_text"]
        )
        hero = match_dna["hero"]
        antihero = match_dna["antihero"]

        path = build_match_dna_share_card(
            home_team=match.home_team.name, away_team=match.away_team.name,
            home_score=match.home_score or 0, away_score=match.away_score or 0,
            drama_level=match_dna["drama_level"], drama_index=match_dna["drama_index"],
            hero_name=str(hero["player"]) if hero else "", hero_score=hero["score"] if hero else None,
            antihero_name=str(antihero["player"]) if antihero else "",
            antihero_score=antihero["score"] if antihero else None,
            fan_mood_text=match_dna["fan_mood_text"],
            consensus_text=match_dna["consensus_text"],
            headline=headline,
        )
        return redirect(default_storage.url(path))


class StreakShareCardView(View):
    """/share/streak/<username>/<streak_type>/card.png — карточка серии.
    Число берём из БД, не из URL.
    """

    def get(self, request, username, streak_type):
        from core.services.share_cards import build_streak_share_card

        if streak_type not in ("evaluation", "prediction"):
            raise Http404("Unknown streak_type")

        user = get_object_or_404(User, username=username, is_profile_public=True)
        streak_count = user.evaluation_streak if streak_type == "evaluation" else user.prediction_streak
        if streak_count <= 0:
            raise Http404("No active streak")

        path = build_streak_share_card(username=user.username, streak_type=streak_type, streak_count=streak_count)
        return redirect(default_storage.url(path))


def handler_404(request, exception):
    return render(request, 'errors/404.html', status=404)


def handler_403(request, exception=None):
    return render(request, 'errors/403.html', status=403)


def handler_500(request):
    return render(request, 'errors/500.html', status=500)