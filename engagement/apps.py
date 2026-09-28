from django.apps import AppConfig


class EngagementConfig(AppConfig):
    name = 'engagement'
    verbose_name = 'Вовлечение'

    def ready(self):
        import engagement.signals  # noqa: F401
