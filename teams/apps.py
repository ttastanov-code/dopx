from django.apps import AppConfig


class TeamsConfig(AppConfig):
    name = 'teams'

    def ready(self):
        # Регистрирует post_save(Team) -> автоматический расчёт
        # фирменного цвета клуба (см. teams/signals.py).
        import teams.signals  # noqa: F401
