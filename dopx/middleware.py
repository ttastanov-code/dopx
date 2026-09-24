# dopx/middleware.py

import re
import time
import logging
from datetime import datetime
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth import logout
from django.db import connection
from django.core.cache import cache
from django.http import HttpResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone

logger = logging.getLogger('django.performance')
security_logger = logging.getLogger('django.security')


class QueryCountMiddleware:
    """Счётчик запросов к БД."""
    
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        initial_queries = len(connection.queries)
        start_time = time.time()
        
        response = self.get_response(request)
        
        duration = time.time() - start_time
        queries = len(connection.queries) - initial_queries
        
        if duration > 0.5 or queries > 50:
            logger.warning(
                f"SLOW/HIGH-QUERY REQUEST: {request.method} {request.path} | "
                f"Duration: {duration:.3f}s | Queries: {queries}"
            )
        
        # Отладочные заголовки
        if request.user.is_staff:
            response['X-Query-Count'] = str(queries)
            response['X-Request-Duration'] = f"{duration:.3f}"
        
        return response


class CacheHitMiddleware:
    """Статистика cache hit/miss."""
    
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Сбрасываем статистику кэша
        cache.hits = 0
        cache.misses = 0
        
        response = self.get_response(request)
        
        total = cache.hits + cache.misses
        if total > 0:
            hit_rate = (cache.hits / total) * 100
            if hit_rate < 50:
                logger.warning(
                    f"LOW CACHE HIT RATE: {request.path} | "
                    f"Hit Rate: {hit_rate:.1f}% ({cache.hits}/{total})"
                )

        return response


class ContentSecurityPolicyMiddleware:
    """Content-Security-Policy.

    Скрипты/стили только с перечисленных доменов. 'unsafe-inline' пока нужен
    из-за инлайновых <script>/style=. 'unsafe-eval' убран — Alpine в CSP-сборке,
    компоненты регистрируются в static/js/alpine-components.js (не x-data="{...}").
    /admin/ (unfold) получает ADMIN_POLICY с 'unsafe-eval' — его htmx использует eval.
    CSP_REPORT_ONLY=True — только логировать нарушения.
    """

    POLICY = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; "
        "font-src 'self' https://cdn.jsdelivr.net https://fonts.gstatic.com; "
        "img-src 'self' data: https:; "
        "connect-src 'self' https://cdn.jsdelivr.net; "
        "worker-src 'self'; "
        "manifest-src 'self'; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'self';"
    )

    # Только для /admin/ (unfold).
    ADMIN_POLICY = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; "
        "font-src 'self' https://cdn.jsdelivr.net https://fonts.gstatic.com; "
        "img-src 'self' data: https:; "
        "connect-src 'self' https://cdn.jsdelivr.net; "
        "worker-src 'self'; "
        "manifest-src 'self'; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'self';"
    )
    ADMIN_PATH_PREFIX = '/admin/'

    # Для embed-виджетов — разрешаем встраивание в чужие iframe.
    WIDGET_POLICY_BASE = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; "
        "font-src 'self' https://cdn.jsdelivr.net https://fonts.gstatic.com; "
        "img-src 'self' data: https:; "
        "connect-src 'self' https://cdn.jsdelivr.net; "
        "worker-src 'self'; "
        "manifest-src 'self'; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "form-action 'self'; "
    )

    @staticmethod
    def _widget_policy() -> str:
        allowed = getattr(settings, 'WIDGET_ALLOWED_ORIGINS', [])
        frame_ancestors = ' '.join(allowed) if allowed else '*'
        return f"{ContentSecurityPolicyMiddleware.WIDGET_POLICY_BASE}frame-ancestors {frame_ancestors};"
    # Точные пути виджетов, а не startswith('/widget').
    WIDGET_PATH_PATTERN = re.compile(
        r'^/(players/[0-9a-f-]+/widget|teams/[0-9a-f-]+/widget|widget/standings'
        r'|season/best-xi/widget|season/[0-9a-f-]+/best-xi/widget'
        r'|season/round/widget|season/[0-9a-f-]+/round/\d+/widget)/$'
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        header = (
            'Content-Security-Policy-Report-Only'
            if getattr(settings, 'CSP_REPORT_ONLY', False)
            else 'Content-Security-Policy'
        )
        if request.path.startswith(self.ADMIN_PATH_PREFIX):
            policy = self.ADMIN_POLICY
        elif self.WIDGET_PATH_PATTERN.match(request.path):
            policy = self._widget_policy()
        else:
            policy = self.POLICY
        response[header] = policy
        return response


class StaffSessionSecurityMiddleware:
    """Idle-таймаут сессии staff только на /admin/ и /staff/dashboard/.
    Простой дольше STAFF_SESSION_IDLE_TIMEOUT_SECONDS — logout и редирект на вход.
    Для htmx-запросов — HX-Redirect, чтобы не подменять фрагмент страницей логина.
    Ставить после AuthenticationMiddleware и OTPMiddleware.
    """

    SESSION_KEY = '_staff_last_activity'
    ENFORCED_PATH_PREFIXES = ('/admin/', '/staff/dashboard/')

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, 'user', None)
        is_enforced_path = request.path.startswith(self.ENFORCED_PATH_PREFIXES)
        if is_enforced_path and user is not None and getattr(user, 'is_authenticated', False) and getattr(user, 'is_staff', False):
            timeout = getattr(settings, 'STAFF_SESSION_IDLE_TIMEOUT_SECONDS', 1800)
            now = timezone.now()
            last_activity_iso = request.session.get(self.SESSION_KEY)

            if last_activity_iso:
                # Битая метка активности — считаем сессию истёкшей.
                try:
                    last_activity = datetime.fromisoformat(last_activity_iso)
                    idle_seconds = (now - last_activity).total_seconds()
                    is_timed_out = idle_seconds > timeout
                except (TypeError, ValueError):
                    idle_seconds = None
                    is_timed_out = True

                if is_timed_out:
                    if idle_seconds is None:
                        security_logger.warning(
                            f"STAFF SESSION TIMEOUT: user={user.username} "
                            f"reason=corrupted_last_activity path={request.path}"
                        )
                    else:
                        security_logger.warning(
                            f"STAFF SESSION TIMEOUT: user={user.username} idle={idle_seconds:.0f}s "
                            f"limit={timeout}s path={request.path}"
                        )
                    logout(request)
                    # next= — вернуть на исходную страницу после входа.
                    login_url = reverse('admin:login')
                    next_qs = urlencode({'next': request.get_full_path()})
                    target = f"{login_url}?{next_qs}&session_expired=1"

                    # htmx — через HX-Redirect.
                    if request.headers.get('HX-Request') == 'true':
                        response = HttpResponse(status=200)
                        response['HX-Redirect'] = target
                        return response

                    return redirect(target)

            request.session[self.SESSION_KEY] = now.isoformat()

        return self.get_response(request)