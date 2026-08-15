# core/management/commands/setup_push_keys.py
"""
Ручная/explicit точка входа для генерации VAPID-ключей (продуктовый аудит,
раздел 5c "PWA + Web Push"). С тех пор, как `core/apps.py::CoreConfig.
ready()` подключил `ensure_vapid_keys_on_startup` к сигналу `post_migrate`,
эта команда для ОБЫЧНОГО сценария (первый деплой, ключей ещё нет) больше
НЕ обязательна — ключи сгенерируются сами при первом `python manage.py
migrate`. Команда остаётся полезной для двух случаев:

1. Явно посмотреть в консоли, что именно произошло/произойдёт, не
   прогоняя миграции.
2. `--force` — осознанный перевыпуск ключей (например, подозрение на
   утечку `.vapid/private_key.pem`). Это ЛОМАЕТ все существующие
   push-подписки пользователей — намеренно НЕ делается автоматически
   сигналом `post_migrate`, только явным флагом здесь.
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

        private_key_path, _ = generate_and_persist_vapid_keys()

        self.stdout.write(self.style.SUCCESS(
            f"Готово: приватный ключ сохранён в {private_key_path}, "
            f"VAPID_PRIVATE_KEY/VAPID_PUBLIC_KEY записаны в "
            f"{settings.BASE_DIR}/.env.\n"
            "Перезапустите сервер (env-переменные читаются один раз при "
            "старте) — после этого push-уведомления заработают."
        ))
