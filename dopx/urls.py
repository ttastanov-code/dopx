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

# Схема/доки API — только staff (IsAdminUser). Публичного API для внешних
# интеграторов нет, список эндпоинтов в открытом доступе — готовая
# шпаргалка для ботов, автоматизирующих накрутку голосов.
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
    # «Живая сборная сезона» — /season/best-xi/ (активный сезон по
    # умолчанию) и /season/<uuid>/best-xi/ (конкретный сезон/лига).
    path('season/', include('season_squad.urls')),
    # «DOPX Лучшие тура» — /season/round/ (последний завершённый тур
    # активного сезона) и /season/<uuid>/round/<tour>/ — та же логика
    # умолчания, отдельное приложение round_squad (см. докстринг
    # round_squad/models.py про отличие от season_squad).
    path('season/', include('round_squad.urls')),
    path('notifications/', include('notifications.urls')),
    path('api/', include('api.urls')),
    path('analytics/', include('analytics.urls')),
    # namespace 'events' — не путать с matches:events (лента ВСЕХ событий матча).
    path('events/', include('events.urls')),
    # Краудсорс-прогноз 1X2 (Sofascore-style) — отдельное приложение
    # predictions/, тот же принцип разделения, что и у events/.
    path('predictions/', include('predictions.urls')),
    # Партнёрская инфраструктура: /go/<slug>/ (реферальная ссылка) и
    # /ad/<uuid>/click/ (клик по баннеру) — короткие корневые пути
    # намеренно, а не /partners/go/<slug>/: партнёр публикует эту ссылку
    # у себя, лишний сегмент в URL не добавляет ничего кроме длины.
    path('', include('partners.urls')),
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
    # Проверка по INSTALLED_APPS, а не по settings.DEBUG: urls.py грузится
    # лениво, при первом резолве URL, что в тестах происходит уже ПОСЛЕ
    # того, как test runner форсит DEBUG=False — settings.DEBUG здесь и в
    # settings.py (где решается MIDDLEWARE) давали бы разный ответ в разный
    # момент времени, и middleware пытался бы рендерить несуществующий 'djdt:...'.
    import debug_toolbar
    urlpatterns = [path('__debug__/', include('debug_toolbar.urls'))] + urlpatterns

urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

handler404 = 'core.views.handler_404'
handler500 = 'core.views.handler_500'