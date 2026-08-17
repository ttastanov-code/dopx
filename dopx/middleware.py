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


class ContentSecurityPolicyMiddleware:
    """
    CSP как собственный middleware, не через django-csp — ради одного
    заголовка тянуть отдельный пакет избыточно (тот же принцип, что и у
    is_rate_limited в core/utils.py вместо django-ratelimit).

    Источники ограничены явным списком доменов (CDN-провайдеры,
    Google Fonts) — это блокирует загрузку скриптов/стилей с ЛЮБОГО
    домена, кроме перечисленных, даже если атакующий найдёт способ
    внедрить `<script src="...">` через XSS. Дополняет, а не заменяет
    SRI на CDN-тегах (templates/base.html, base_auth.html) — SRI защищает
    от подмены файла НА уже доверенном домене (компрометация CDN), CSP —
    от загрузки С НЕДОверенного домена.

    `'unsafe-inline'` в script-src/style-src — вынужденный компромисс:
    проект держит инлайновые <script> и onclick=/x-data= по всем шаблонам
    (HTMX-колбэки, Alpine-компоненты, инлайновые градиенты через style=).
    Без него страницы посыпались бы массово. Полное закрытие потребовало
    бы переписать все инлайн-скрипты на nonce/hash — отдельная задача,
    не блокирующая этот шаг (сама по себе политика всё равно валит
    внешние <script src="evil.com/x.js">, инъекцию через <base>,
    встраивание сайта в чужой <iframe> и т.д.).

    `'unsafe-eval'` в script-src — ОБЯЗАТЕЛЕН для Alpine.js в том виде, в
    котором он используется в проекте (x-data="{...}" с inline-выражениями
    прямо в HTML-атрибутах, без сборки). Alpine компилирует каждое
    x-data/x-show/x-on/x-text выражение через `new AsyncFunction(...)`
    (см. alpinejs/dist/cdn.min.js) — это ЕСТЬ eval с точки зрения CSP,
    отдельно от inline-скриптов, которые покрывает 'unsafe-inline'. Без
    'unsafe-eval' каждое Alpine-выражение на странице падает молча в
    консоль (Alpine Expression Error) — тема/дропдаун уведомлений/
    мобильное меню/scroll-to-top/cookie-баннер и любой x-show/@click
    перестают работать целиком, при этом HTML рендерится нормально
    (это и произошло при первом включении CSP — стили вернулись, но вся
    Alpine-интерактивность отвалилась). Единственная альтернатива —
    переход на CSP-safe сборку Alpine (@alpinejs/csp), которая запрещает
    именно inline x-data-выражения и требует регистрировать компоненты
    через Alpine.data(...) в обычном JS-файле — отдельный рефакторинг всех
    шаблонов, не блокирующий этот шаг.

    CSP_REPORT_ONLY=True (settings.py) переключает заголовок на
    Content-Security-Policy-Report-Only — браузер логирует нарушения в
    консоль, но ничего не блокирует. Полезно перед первым включением на
    проде, чтобы отловить забытый домен, не уронив сайт.
    """

    POLICY = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; "
        "font-src 'self' https://cdn.jsdelivr.net https://fonts.gstatic.com; "
        "img-src 'self' data: https:; "
        "connect-src 'self'; "
        "worker-src 'self'; "
        "manifest-src 'self'; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'self';"
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
        response[header] = self.POLICY
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