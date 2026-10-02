from django.apps import AppConfig


class DashboardConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'dashboard'
    verbose_name = 'Staff-дашборд'

    def ready(self):
        # Админки auth/otp/axes уже зарегистрированы (autodiscover в admin.ready).
        from django.contrib import admin

        from core.admin_mixins import lock_superuser_only_models

        lock_superuser_only_models(admin.site)
