from django.apps import AppConfig


class AdminbotConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "adminbot"
    verbose_name = "Telegram-бот администрирования"

    def ready(self):
        import adminbot.signals  # noqa: F401
