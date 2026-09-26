from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = 'core'
    verbose_name = 'Платформа'

    def ready(self):
        # Генерация VAPID-ключей на post_migrate (один раз за прогон, sender=self).
        from django.db.models.signals import post_migrate

        from core.services.vapid import ensure_vapid_keys_on_startup

        post_migrate.connect(ensure_vapid_keys_on_startup, sender=self)
