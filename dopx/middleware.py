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
    """Sliding idle-таймаут сессии ТОЛЬКО для staff (is_staff=True) —
    продуктовый аудит "защита на высшем уровне". Обычный посетитель сайта
    держит сессию все SESSION_COOKIE_AGE (2 недели, см. settings.py) — это
    нормальный UX. Но сотрудник с открытой вкладкой /admin/ или
    /staff/dashboard/, отошедший от компьютера, не должен оставлять живую
    сессию с доступом к антифроду/парсеру/PII на сколько угодно долго.

    Механика: при КАЖДОМ запросе от staff пишем метку последней активности в
    саму сессию (`_staff_last_activity`). Если между запросами прошло больше
    STAFF_SESSION_IDLE_TIMEOUT_SECONDS (по умолчанию 30 минут,
    dopx/settings.py) — сессия принудительно убивается через logout() и
    редирект на страницу входа. НЕ используем глобальный SESSION_COOKIE_AGE
    для этого: он бьёт по ВСЕМ пользователям одинаково, а не только по staff.

    ВАЖНО: должен стоять В MIDDLEWARE ПОСЛЕ AuthenticationMiddleware (нужен
    request.user) и ПОСЛЕ OTPMiddleware, если 2FA включена — иначе logout()
    здесь может конфликтовать с OTP-состоянием сессии.
    """

    SESSION_KEY = '_staff_last_activity'

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, 'user', None)
        if user is not None and getattr(user, 'is_authenticated', False) and getattr(user, 'is_staff', False):
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