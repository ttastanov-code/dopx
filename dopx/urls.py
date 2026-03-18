# dopx/urls.py
from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView, SpectacularRedocView

# Schema views с ЯВНЫМ отключением throttle
schema_patterns = [
    path('api/schema/', SpectacularAPIView.as_view(
        throttle_classes=[]  # 🔥 Отключаем throttle для OpenAPI schema
    ), name='schema'),
    path('api/docs/', SpectacularSwaggerView.as_view(
        url_name='schema',
        throttle_classes=[]  # 🔥 Отключаем throttle для Swagger UI
    ), name='swagger-ui'),
    path('api/redoc/', SpectacularRedocView.as_view(
        url_name='schema',
        throttle_classes=[]  # 🔥 Отключаем throttle для ReDoc
    ), name='redoc'),
]

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/', include('api.urls')),
] + schema_patterns  # 👈 Добавляем schema patterns

if settings.DEBUG:
    import debug_toolbar
    urlpatterns = [
        path('__debug__/', include('debug_toolbar.urls')),
    ] + urlpatterns