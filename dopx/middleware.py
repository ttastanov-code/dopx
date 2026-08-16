# dopx/middleware.py — Query Performance Middleware + Security

import time
import logging
from datetime import datetime

from django.conf import settings
from django.contrib.auth import logout
from django.db import connection
from django.core.cache import cache
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone

logger = logging.getLogger('django.performance')
security_logger = logging.getLogger('django.security')


class QueryCountMiddleware:
    """Подсчёт количества запросов к БД"""
    
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
        
        # Добавляем заголовки для отладки
        if request.user.is_staff:
            response['X-Query-Count'] = str(queries)
            response['X-Request-Duration'] = f"{duration:.3f}"
        
        return response


class CacheHitMiddleware:
    """Мониторинг cache hit/miss"""
    
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


class StaffSessionSecurityMiddleware:
    """
    Sliding idle-таймаут сессии для staff, но ТОЛЬКО на /admin/ и
    /staff/dashboard/ — обычный посетитель (и staff вне панелей управления)
    живёт весь SESSION_COOKIE_AGE (2 недели). Раньше проверка срабатывала на
    ЛЮБОЙ странице сайта: staff-аккаунт, зашедший на публичную страницу
    матча и не слав запросов 30+ минут (задний план вкладки на телефоне,
    экран заблокирован — HTMX-поллинг в фоне браузеры глушат), при
    следующем действии получал logout() + редирект вместо обработки самого
    запроса — например, тап по реакции на событие молча пропадал вместо
    сохранения в БД. Метка активности пишется в сессию (_staff_last_activity)
    на каждый запрос к панелям; простой дольше
    STAFF_SESSION_IDLE_TIMEOUT_SECONDS (30 мин по умолчанию) — logout() и
    редирект на вход.

    Должен стоять в MIDDLEWARE после AuthenticationMiddleware (нужен
    request.user) и после OTPMiddleware, если включена 2FA.
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
                try:
                    last_activity = datetime.fromisoformat(last_activity_iso)
                except (TypeError, ValueError):
                    last_activity = now
                idle_seconds = (now - last_activity).total_seconds()
                if idle_seconds > timeout:
                    security_logger.warning(
                        f"STAFF SESSION TIMEOUT: user={user.username} idle={idle_seconds:.0f}s "
                        f"limit={timeout}s path={request.path}"
                    )
                    logout(request)
                    return redirect(f"{reverse('admin:login')}?session_expired=1")

            request.session[self.SESSION_KEY] = now.isoformat()

        return self.get_response(request)