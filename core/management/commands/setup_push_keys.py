# core/management/commands/setup_push_keys.py
"""
Ручная точка входа для генерации VAPID-ключей. core/apps.py::CoreConfig.
ready() уже генерирует их автоматически на post_migrate, так что команда
не обязательна для обычного деплоя — полезна для явного вывода в консоль
и для --force (осознанный перевыпуск, например при утечке
.vapid/private_key.pem). --force ломает все существующие push-подписки,
поэтому не делается автоматически сигналом.
"""
from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand

from core.services.vapid import generate_and_persist_vapid_keys, vapid_keys_configured


class Command(BaseCommand):
    help = (
        "Генерирует VAPID-ключи для Web Push, если их ещё нет (обычно в "
        "этом уже нет необходимости — см. core/apps.py::CoreConfig.ready(), "
        "ключи генерируются автоматически на python manage.py migrate). "
        "Используйте --force для осознанного перевыпуска."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--force',
            action='store_true',
            help=(
                "Перегенерировать ключи, даже если они уже настроены. "
                "ЛОМАЕТ все существующие push-подписки пользователей "
                "(users.PushSubscription) — им придётся включить "
                "уведомления заново."
            ),
        )

    def handle(self, *args, **options):
        if vapid_keys_configured() and not options['force']:
            self.stdout.write(self.style.SUCCESS(
                "VAPID-ключи уже настроены — пропуск. Передайте --force "
                "для перегенерации (сломает существующие push-подписки)."
            ))
            return

        # generate_and_persist_vapid_keys() возвращает (raw base64url
        # приватный ключ, application server key) — первое значение НЕ
        # выводим в консоль (это секрет, а не путь, см. БАГ в
        # core/services/vapid.py::generate_and_persist_vapid_keys).
        generate_and_persist_vapid_keys()

        self.stdout.write(self.style.SUCCESS(
            f"Готово: VAPID_PRIVATE_KEY/VAPID_PUBLIC_KEY записаны в "
            f"{settings.BASE_DIR}/.env (PEM-копия приватного ключа — в "
            f"{settings.BASE_DIR}/.vapid/private_key.pem, для отладки/бэкапа).\n"
            "Перезапустите сервер (env-переменные читаются один раз при "
            "старте) — после этого push-уведомления заработают."
        ))
