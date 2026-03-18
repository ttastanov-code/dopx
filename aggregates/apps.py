# aggregates/apps.py
from django.apps import AppConfig

class AggregatesConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'aggregates'
    
    def ready(self):
        # Импортируем сигналы при готовности приложения
        import aggregates.signals