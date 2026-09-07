# dopx/middleware.py — Query Performance Middleware + Security

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

    `ADMIN_POLICY` (2026-08-21, живой баг-репорт пользователя: "зашёл в
    админку, там окно [Available shortcuts] и оно не закрывается") —
    Django `/admin/` (тема django-unfold) держит СОБСТВЕННЫЙ бандл htmx
    (`unfold/static/unfold/js/htmx/htmx.js`, отдельный от `htmx.org@2.0.8`,
    который сайт грузит в templates/base.html) — этот бандл использует
    `new Function(...)`/`eval(...)` внутри себя (см. `hx-on:`-механизм
    htmx — инлайновый JS-обработчик в атрибуте компилируется именно так).
    Убранный ниже `'unsafe-eval'` ломает это в /admin/: клавиатурная
    командная палитра Unfold (открывается на `?`, "Available shortcuts")
    перестаёт закрываться по ESC/клику вне — обработчик молча не создаётся,
    браузер тихо блокирует `new Function(...)` без видимого пользователю
    сообщения об ошибке (в консоли DevTools при этом обычно ЕСТЬ
    "Refused to evaluate a string... unsafe-eval", если открыть её).
    Проверено статическим анализом (grep по всем unfold/static/**/*.js —
    `alpine.js` от Unfold САМ по себе eval не использует, `chart.js` и
    `htmx.js` — используют). Патчить бандлы третьей стороны (`app_venv/`)
    неправильно — вместо этого /admin/ получает ОТДЕЛЬНУЮ, чуть более
    мягкую политику с `'unsafe-eval'` обратно. `/staff/dashboard/` (наш
    собственный staff-тулинг) сюда НЕ входит — те шаблоны наследуются от
    ТОГО ЖЕ templates/base.html, что и публичный сайт (см. `{% extends
    'base.html' %}` в templates/dashboard/*.html), используют ту же
    CSP-safe сборку Alpine и НЕ нуждаются в послаблении.

    `'unsafe-eval'` УБРАН из основной POLICY (2026-08-21). Раньше был обязателен,
    потому что Alpine.js компилировал каждое x-data/x-show/x-on/x-text
    выражение через `new AsyncFunction(...)` (см. alpinejs/dist/cdn.min.js)
    — это ЕСТЬ eval с точки зрения CSP, отдельно от инлайновых <script>,
    которые покрывает 'unsafe-inline'. Устранено переходом на CSP-safe
    сборку Alpine (@alpinejs/csp, см. templates/base.html/base_auth.html) —
    она в принципе не использует new Function()/eval, но взамен НЕ
    поддерживает инлайновые объектные литералы в x-data="{...}". Поэтому
    ВСЕ x-data по шаблонам переведены на зарегистрированные компоненты
    (Alpine.data(...) в static/js/alpine-components.js), а x-data в HTML
    теперь везде выглядит как x-data="имяКомпонента" / x-data="имяКомпонента(аргумент)".
    Если добавляете новый Alpine-компонент — регистрируйте его ТАМ, а не
    инлайновым объектным литералом, иначе он молча не заработает под этой
    сборкой (никакого "просто добавь unsafe-eval обратно" — это осознанный
    откат небезопасного флага).

    `'unsafe-inline'` в script-src/style-src остаётся — проект всё ещё
    держит инлайновые <script> и style= по части шаблонов (HTMX-колбэки,
    инлайновые градиенты). Полное закрытие требует nonce/hash-рефакторинга
    каждого такого места — отдельная задача, см. docs/BACKLOG.md.

    CSP_REPORT_ONLY=True (settings.py) переключает заголовок на
    Content-Security-Policy-Report-Only — браузер логирует нарушения в
    консоль, но ничего не блокирует. ОБЯЗАТЕЛЬНО включить это перед первым
    деплоем данного изменения и живьём в браузере убедиться, что ни один
    Alpine-компонент не даёт "Alpine Expression Error" / CSP violation в
    консоли — эта сборка не тестировалась в реальном браузере из песочницы,
    в которой велась разработка (см. docs/BACKLOG.md).
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

    # Только для Django /admin/ (django-unfold) — см. докстринг выше про
    # собственный бандл htmx у Unfold, которому нужен 'unsafe-eval'.
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

    # Отдельная CSP-политика для embed-виджетов — frame-ancestors 'self' на
    # общей политике блокировал партнёрские iframe несмотря на
    # @xframe_options_exempt. См. docs/adr/0016-widget-csp-frame-ancestors.md.
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
    # Точечные альтернативы в regex, не общий startswith('/widget') — иначе
    # риск случайно ослабить frame-ancestors на будущей несвязанной странице.
    # См. docs/adr/0016-widget-csp-frame-ancestors.md.
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

    2026-08-24, живой баг-репорт пользователя: "когда происходит
    автоматический логаут, экран остаётся как будто залогинен, потом
    выходят разные ошибки, если оставил окно открытым в админке/
    дашборде". Причина: и /staff/dashboard/, и unfold-тема /admin/
    активно используют htmx (см. ContentSecurityPolicyMiddleware —
    фоновый поллинг hx-trigger="every Ns" на data_health/parser_tools и
    т.д., плюс собственный бандл htmx у unfold). htmx делает fetch()
    САМ и просто получает финальный ответ после редиректа — обычный
    Django redirect() на /admin/login/ превращался в HTML страницы
    логина, который htmx ПОДМЕНЯЛ ВНУТРЬ фрагмента (таблицу/карточку),
    ломая DOM (вложенный <html> внутри <div>, задвоенные id, CSRF-токен
    вне контекста) — отсюда "разные ошибки" после. Раньше это
    оставалось незамеченным, пока пользователь не кликал руками — а
    фоновый поллинг срабатывает САМ каждые 10-15с независимо от клика,
    так что дырка проявлялась быстро и без явного триггера от юзера.

    Фикс: для htmx-запросов (заголовок HX-Request, его шлёт ЛЮБОЙ
    htmx-бандл, включая собственный у unfold) отдаём НЕ redirect(), а
    200 с заголовком HX-Redirect — это встроенный механизм htmx: браузер
    делает ПОЛНУЮ навигацию (window.location), а не подмену фрагмента.
    В худшем случае пользователь видит логин-страницу через ближайший
    тик фонового поллинга (те самые 10-15с) вместо зависшего "как будто
    залогинен" экрана навсегда.
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
                # БАГ, КОТОРЫЙ ТУТ БЫЛ: при TypeError/ValueError от парсинга
                # (повреждённое/подделанное значение _staff_last_activity в
                # сессии) код тихо делал `last_activity = now` — то есть
                # ПОДАРИВАЛ staff-пользователю с битой сессией свежий отсчёт
                # простоя вместо разлогинивания. Fail-open там, где должен
                # быть fail-closed: битые данные сессии обрабатываем так же,
                # как истёкший idle-таймаут — logout() и редирект на вход,
                # а не льготный сброс таймера.
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
                    # БАГ, КОТОРЫЙ ТУТ БЫЛ: редирект на admin:login нёс только
                    # ?session_expired=1, без next= — в отличие от обычного
                    # незалогиненного захода (тот next добавляет сам
                    # staff_member_required через redirect_to_login). После
                    # такого logout()+редиректа Django после успешного входа
                    # брал ЖЁСТКИЙ дефолт settings.LOGIN_REDIRECT_URL
                    # ("/accounts/profile/", которого в проекте нет) —
                    # 404 вместо возврата на страницу, с которой юзера сняли
                    # по простою. Симптом был неуловим именно потому, что
                    # обычный вход (без предварительного idle-таймаута)
                    # отрабатывал корректно — баг только в ЭТОЙ ветке.
                    login_url = reverse('admin:login')
                    next_qs = urlencode({'next': request.get_full_path()})
                    target = f"{login_url}?{next_qs}&session_expired=1"

                    # htmx (наш /staff/dashboard/ поллинг И собственный
                    # бандл unfold в /admin/) — HX-Redirect вместо обычного
                    # редиректа, иначе htmx подменяет фрагмент на HTML
                    # страницы логина вместо полной навигации (см.
                    # докстринг класса выше).
                    if request.headers.get('HX-Request') == 'true':
                        response = HttpResponse(status=200)
                        response['HX-Redirect'] = target
                        return response

                    return redirect(target)

            request.session[self.SESSION_KEY] = now.isoformat()

        return self.get_response(request)