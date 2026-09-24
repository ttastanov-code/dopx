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

# Схема/доки API — только staff.
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
    # «Живая сборная сезона»: активный сезон или конкретный.
    path('season/', include('season_squad.urls')),
    # «DOPX Лучшие тура»: текущий тур или конкретный.
    path('season/', include('round_squad.urls')),
    path('notifications/', include('notifications.urls')),
    path('api/', include('api.urls')),
    path('analytics/', include('analytics.urls')),
    # namespace 'events' — не путать с matches:events.
    path('events/', include('events.urls')),
    # Прогнозы 1X2.
    path('predictions/', include('predictions.urls')),
    # Партнёры: /go/<slug>/ и /ad/<uuid>/click/ — короткие корневые пути.
    path('', include('partners.urls')),
    # Staff-дашборд (доступ проверяется во вьюхах).
    path('staff/dashboard/', include('dashboard.urls')),
    # sitemap кэшируется на 12 ч.
    path('sitemap.xml', cache_page(60 * 60 * 12)(sitemap), {'sitemaps': sitemaps}, name='sitemap'),
    path('robots.txt', robots_txt, name='robots'),
    # Капча.
    path('captcha/', include('captcha.urls')),
] + schema_patterns

if 'debug_toolbar' in settings.INSTALLED_APPS:
    # Debug Toolbar — по INSTALLED_APPS, не по DEBUG (в тестах DEBUG меняется позже).
    import debug_toolbar
    urlpatterns = [path('__debug__/', include('debug_toolbar.urls'))] + urlpatterns

urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

handler404 = 'core.views.handler_404'
handler500 = 'core.views.handler_500'