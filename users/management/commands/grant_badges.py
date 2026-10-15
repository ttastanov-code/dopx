# users/management/commands/grant_badges.py
"""Выдать достижения вручную (демо, награды от команды). Такие медали пересчёт прогресса не снимает.

    manage.py grant_badges t.tastanov --all
    manage.py grant_badges t.tastanov --code founder --code perfect_tour
    manage.py grant_badges t.tastanov --all --revoke   # снять выданные вручную
"""
from django.core.management.base import BaseCommand, CommandError

from users.badges import BADGE_CATALOG


class Command(BaseCommand):
    help = "Выдать или снять достижения вручную."

    def add_arguments(self, parser):
        parser.add_argument("username")
        parser.add_argument("--code", action="append", default=[], help="Код достижения (можно несколько раз).")
        parser.add_argument("--all", action="store_true", help="Все достижения каталога.")
        parser.add_argument("--revoke", action="store_true", help="Снять выданные вручную.")

    def handle(self, *args, **opts):
        from users.models import User, UserBadge
        from users.prestige import invalidate

        user = User.objects.filter(username=opts["username"]).first()
        if not user:
            raise CommandError(f"Пользователь «{opts['username']}» не найден.")
        codes = list(BADGE_CATALOG) if opts["all"] else opts["code"]
        unknown = [c for c in codes if c not in BADGE_CATALOG]
        if unknown or not codes:
            raise CommandError(f"Неизвестные коды: {', '.join(unknown)}" if unknown else "Укажите --code или --all.")
        if opts["revoke"]:
            n = UserBadge.objects.filter(user=user, badge_type__in=codes, granted_manually=True).delete()[0]
            self.stdout.write(self.style.SUCCESS(f"Снято: {n}"))
        else:
            n = 0
            for code in codes:
                badge, created = UserBadge.objects.get_or_create(user=user, badge_type=code, defaults={"granted_manually": True})
                if not created and (not badge.granted_manually or badge.is_stale):
                    badge.granted_manually, badge.is_stale, badge.stale_since = True, False, None
                    badge.save(update_fields=["granted_manually", "is_stale", "stale_since"])
                n += created
            self.stdout.write(self.style.SUCCESS(f"Выдано новых: {n}, всего у пользователя: {UserBadge.objects.filter(user=user).count()}"))
        invalidate(user.pk)
