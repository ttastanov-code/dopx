# core/views.py
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
from evaluations.models import EvaluationSession, MatchEvaluation, PlayerEvaluation, TeamEvaluation
from matches.models import Match
from seasons.models import Season
from teams.models import Team
from users.models import User

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
        
        match_aggs = MatchAggregate.objects.all()
        avg_entertainment = match_aggs.aggregate(
            avg=Avg('avg_entertainment')
        )['avg'] or 0
        avg_drama = match_aggs.aggregate(
            avg=Avg('drama_index')
        )['avg'] or 0
        
        metrics = {
            'avg_entertainment': round(avg_entertainment, 1),
            'avg_drama': round(avg_drama, 0),
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
        active_match_id = None
        if self.request.user.is_authenticated:
            active_session = EvaluationSession.objects.filter(
                user=self.request.user,
                status__in=['started', 'in_progress']
            ).select_related('match').first()
            if active_session:
                active_match_id = active_session.match.id
        
        context.update({
            'recent_matches': recent_matches,
            'upcoming_matches': upcoming_matches,
            'top_players': top_players,
            'top_teams': top_teams,
            'stats': stats,
            'metrics': metrics,
            'active_match_id': active_match_id,
            'page_title': 'DOPX — Голос трибун измеряем',
            'now': now,
        })
        return context


def standings_preview(request):
    """HTMX partial для превью турнирной таблицы"""
    season = Season.objects.filter(is_active=True).first()
    if not season:
        return HttpResponse('''
        <div class="text-center py-8 opacity-60">
            <i class="ti ti-trophy-off text-3xl mb-2"></i>
            <p class="text-sm">Нет активного сезона</p>
        </div>
        ''')
    
    # ✅ КЭШИРОВАНИЕ (используем тот же ключ что в LeagueDetailView)
    cache_key = f'league_{season.league.id}_season_{season.id}_standings_preview'
    cached_html = cache.get(cache_key)
    
    if cached_html:
        return HttpResponse(cached_html)
    
    teams = Team.objects.filter(
        teamseason__season=season,
        is_active=True
    ).distinct()
    
    standings_list = []
    for team in teams:
        # ✅ ОПТИМИЗАЦИЯ: один SQL запрос вместо 6+
        stats = Match.objects.filter(
            season=season,
            status='finished'
        ).aggregate(
            home_played=Count('id', filter=Q(home_team=team)),
            away_played=Count('id', filter=Q(away_team=team)),
            home_wins=Count('id', filter=Q(home_team=team) & Q(home_score__gt=F('away_score'))),
            away_wins=Count('id', filter=Q(away_team=team) & Q(away_score__gt=F('home_score'))),
            home_draws=Count('id', filter=Q(home_team=team) & Q(home_score=F('away_score'))),
            away_draws=Count('id', filter=Q(away_team=team) & Q(away_score=F('home_score'))),
            home_goals_scored=Sum('home_score', filter=Q(home_team=team)),
            away_goals_scored=Sum('away_score', filter=Q(away_team=team)),
            home_goals_conceded=Sum('away_score', filter=Q(home_team=team)),
            away_goals_conceded=Sum('home_score', filter=Q(away_team=team)),
        )
        
        played = (stats['home_played'] or 0) + (stats['away_played'] or 0)
        wins = (stats['home_wins'] or 0) + (stats['away_wins'] or 0)
        draws = (stats['home_draws'] or 0) + (stats['away_draws'] or 0)
        losses = played - wins - draws
        
        goals_scored = ((stats['home_goals_scored'] or 0) + (stats['away_goals_scored'] or 0))
        goals_conceded = ((stats['home_goals_conceded'] or 0) + (stats['away_goals_conceded'] or 0))
        goal_diff = goals_scored - goals_conceded
        points = wins * 3 + draws
        
        # ✅ ТОТ ЖЕ ФОРМАТ что в LeagueDetailView
        standings_list.append({
            'team_id': str(team.id),
            'team_name': team.name,
            'team_logo_url': team.logo_url,
            'played': played,
            'wins': wins,
            'draws': draws,
            'losses': losses,
            'goals_scored': goals_scored,
            'goals_conceded': goals_conceded,
            'goal_diff': goal_diff,
            'points': points,
        })
    
    standings_list.sort(key=lambda x: (-x['points'], -x['goal_diff'], -x['goals_scored']))
    standings_list = standings_list[:10]
    
    html = render_to_string('components/_standings_preview.html', {
        'standings': standings_list,
        'season': season,
    })
    
    # ✅ КЭШИРУЕМ HTML (не данные, а готовый HTML)
    cache.set(cache_key, html, 300)
    
    return HttpResponse(html)


class RulesView(TemplateView):
    """Страница с правилами платформы"""
    template_name = 'core/rules.html'
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_title'] = 'Правила платформы — DOPX'
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
                ip_address=self.get_client_ip(request),
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
    
    def get_client_ip(self, request):
        x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
        if x_forwarded_for:
            ip = x_forwarded_for.split(',')[0]
        else:
            ip = request.META.get('REMOTE_ADDR')
        return ip
    
    def send_admin_notification(self, submission):
        """Отправка уведомления админу"""
        admin_email = getattr(settings, 'CONTACT_EMAIL', 'admin@dopx.kz')
        from_email = getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@dopx.kz')
        site_url = getattr(settings, 'SITE_URL', 'https://dopx.kz')
        
        subject = f"📬 Новое обращение #{str(submission.id)[:8]} ({submission.get_category_display()})"
        
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


def handler_404(request, exception):
    return render(request, 'errors/404.html', status=404)


def handler_500(request):
    return render(request, 'errors/500.html', status=500)