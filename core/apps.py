from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = 'core'

    def ready(self):
        # Продуктовый аудит, раздел 5c ("PWA + Web Push"): автогенерация
        # VAPID-ключей на КАЖДОМ `python manage.py migrate`, если их ещё
        # нет — см. docstring core/services/vapid.py::
        # ensure_vapid_keys_on_startup про то, почему именно post_migrate,
        # а не просто код в ready() (который выполняется на каждый вызов
        # ЛЮБОЙ management-команды, включая makemigrations/test/shell —
        # слишком часто и не по делу для операции, которая пишет файлы на
        # диск). `sender=self` — сработает ровно один раз за прогон
        # migrate, а не по разу на каждое из ~20 приложений проекта.
        from django.db.models.signals import post_migrate

        from core.services.vapid import ensure_vapid_keys_on_startup

        post_migrate.connect(ensure_vapid_keys_on_startup, sender=self)
