# dopx/urls.py
from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView, SpectacularRedocView

schema_patterns = [
    path('api/schema/', SpectacularAPIView.as_view(throttle_classes=[]), name='schema'),
    path('api/docs/', SpectacularSwaggerView.as_view(url_name='schema', throttle_classes=[]), name='swagger-ui'),
    path('api/redoc/', SpectacularRedocView.as_view(url_name='schema', throttle_classes=[]), name='redoc'),
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
    # Self-hosted CAPTCHA (django-simple-captcha) — картинка + refresh-эндпоинт.
    path('captcha/', include('captcha.urls')),
] + schema_patterns

if settings.DEBUG:
    import debug_toolbar
    urlpatterns = [path('__debug__/', include('debug_toolbar.urls'))] + urlpatterns

urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

handler404 = 'core.views.handler_404'
handler500 = 'core.views.handler_500'