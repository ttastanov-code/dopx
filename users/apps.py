# === В файле: users/apps.py ===
from django.apps import AppConfig

class UsersConfig(AppConfig):
    name = 'users'
    verbose_name = 'Пользователи'
    
    def ready(self):
        import users.signals