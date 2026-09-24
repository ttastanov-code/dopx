# notifications/management/commands/send_test_push.py
"""manage.py send_test_push <username> [--kind match_event]

Отправляет тестовый push на все устройства пользователя и показывает результат по каждому.
"""
from django.core.management.base import BaseCommand, CommandError

from notifications.services import PUSH_PROFILES, send_push_to_users
from users.models import PushSubscription, User


class Command(BaseCommand):
    help = "Тестовый push на все устройства пользователя."

    def add_arguments(self, parser):
        parser.add_argument("username")
        parser.add_argument("--kind", default="match_event", choices=sorted(PUSH_PROFILES))

    def handle(self, *args, username, kind, **options):
        user = User.objects.filter(username=username).first()
        if not user:
            raise CommandError(f"Пользователь {username} не найден")

        subs = list(PushSubscription.objects.filter(user=user))
        if not subs:
            self.stdout.write(self.style.WARNING("У пользователя нет push-подписок — включите уведомления в настройках."))
            return
        for s in subs:
            self.stdout.write(f"  устройство: {s.user_agent or '—'}  endpoint={s.endpoint[:60]}…")

        profile = PUSH_PROFILES[kind]
        sent = send_push_to_users(
            [user.id], title="DOPX: тестовое уведомление",
            body=f"Тип {kind}: TTL {profile['ttl']} с, срочность {profile['urgency']}.",
            url="/", kind=kind, tag="test-push",
        )
        left = PushSubscription.objects.filter(user=user).count()
        style = self.style.SUCCESS if sent else self.style.ERROR
        self.stdout.write(style(f"Готово: отправлено {sent} из {len(subs)}, удалено мёртвых подписок: {len(subs) - left}."))
        if not sent:
            self.stdout.write("Проверьте VAPID-ключи в .env и логи воркера (push: …).")
