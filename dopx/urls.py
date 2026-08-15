# dopx/urls.py
from django.contrib import admin
from django.contrib.sitemaps.views import sitemap
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from django.views.decorators.cache import cache_page
from rest_framework.permissions import IsAdminUser
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView, SpectacularRedocView

from core.sitemaps import MatchSitemap, PlayerSitemap, TeamSitemap, CoachSitemap, StaticViewSitemap
from core.views import robots_txt

sitemaps = {
    "matches": MatchSitemap, "players": PlayerSitemap,
    "teams": TeamSitemap, "coaches": CoachSitemap, "static": StaticViewSitemap,
}

# ИЗМЕНЕНО: раньше /api/schema/, /api/docs/ (Swagger) и /api/redoc/ были
# полностью публичными и без авторизации — ссылка "API Документация"
# висела в футере для ЛЮБОГО посетителя сайта. Продукта с публичным API
# для внешних интеграторов нет, а список эндпоинтов и форматов запросов
# в свободном доступе — это просто готовая шпаргалка для ботов, которые
# захотят автоматизировать накрутку голосов (см. антифрод-меры в
# evaluations/views.py и users/views.py). Теперь схема/доки доступны
# только сотрудникам с is_staff=True — сама документация никуда не делась,
# ей просто нужно быть залогиненным в админку под staff-аккаунтом.
schema_patterns = [
    path('api/schema/', SpectacularAPIView.as_view(throttle_classes=[], permission_classes=[IsAdminUser]), name='schema'),
    path('api/docs/', SpectacularSwaggerView.as_view(url_name='schema', throttle_classes=[], permission_classes=[IsAdminUser]), name='swagger-ui'),
    path('api/redoc/', SpectacularRedocView.as_view(url_name='schema', throttle_classes=[], permission_classes=[IsAdminUser]), name='redoc'),
]

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', include('core.urls')),
    path('matches/', include('matches.urls')),
    path('evaluations/', include('evaluations.urls')),
    path('users/', include('users.urls')),
    path('players/', include('players.urls')),
    path('teams/', include('teams.urls')),
    path('coaches/', include('coaches.urls')),
    path('referees/', include('referees.urls')),
    path('leagues/', include('leagues.urls')),
    path('notifications/', include('notifications.urls')),
    path('api/', include('api.urls')),
    path('analytics/', include('analytics.urls')),
    # Live-пульс (продуктовый аудит, раздел 2) — отдельный namespace 'events',
    # не путать с matches:events (match_events_partial, лента ВСЕХ событий).
    path('events/', include('events.urls')),
    # Staff-дашборд (метрики продукта, здоровье KFF-синка, антифрод-очередь).
    # Доступ — staff_member_required на каждой вьюхе (dashboard/views.py),
    # не на уровне URL-конфига, чтобы поведение было явным и тестируемым.
    path('staff/dashboard/', include('dashboard.urls')),
    # SEO: sitemap кэшируется на 12ч — пересчитывать на каждый заход бота
    # бессмысленно, список завершённых матчей/игроков не меняется поминутно.
    path('sitemap.xml', cache_page(60 * 60 * 12)(sitemap), {'sitemaps': sitemaps}, name='sitemap'),
    path('robots.txt', robots_txt, name='robots'),
    # Self-hosted CAPTCHA (django-simple-captcha) — картинка + refresh-эндпоинт.
    path('captcha/', include('captcha.urls')),
] + schema_patterns

if 'debug_toolbar' in settings.INSTALLED_APPS:
    # ИСПРАВЛЕНО (найдено при первом прогоне manage.py test с реальным HTTP-
    # запросом через self.client): раньше здесь стояло `if settings.DEBUG:`
    # — та же проверка, что и в dopx/settings.py при формировании
    # INSTALLED_APPS/MIDDLEWARE, но выполняется она в СОВЕРШЕННО ДРУГОЙ
    # момент времени. INSTALLED_APPS/MIDDLEWARE фиксируются один раз при
    # первой загрузке settings.py (DEBUG там ещё True, как в .env). А
    # dopx/urls.py Django импортирует ЛЕНИВО — при первом реальном
    # разрешении URL, которое в тестах происходит УЖЕ ПОСЛЕ того, как
    # `django.test.utils.setup_test_environment()` принудительно выставил
    # settings.DEBUG=False. Итог: DebugToolbarMiddleware в MIDDLEWARE есть
    # (решение принято раньше, пока DEBUG=True), а `__debug__/` с
    # namespace 'djdt' в urlpatterns — нет (решение принято позже, когда
    # DEBUG уже False) → рассинхрон, middleware пытается отрендерить
    # тулбар и падает NoReverseMatch на 'djdt:...'. Проверка по
    # INSTALLED_APPS вместо повторного settings.DEBUG устраняет расхождение:
    # оба решения (добавлять ли middleware и добавлять ли urls) теперь
    # опираются на ОДНО и то же состояние, зафиксированное в ОДИН момент.
    import debug_toolbar
    urlpatterns = [path('__debug__/', include('debug_toolbar.urls'))] + urlpatterns

urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

handler404 = 'core.views.handler_404'
handler500 = 'core.views.handler_500'