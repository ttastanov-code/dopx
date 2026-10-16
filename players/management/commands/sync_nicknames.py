# players/management/commands/sync_nicknames.py
"""Прозвища игроков из данных поставщика (display_name из одного слова: Жоржиньо, Эдмилсон).
Обычно ставятся сами при импорте; команда — для уже загруженных игроков с составными фамилиями.

    manage.py sync_nicknames            # dry-run
    manage.py sync_nicknames --apply
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Подтянуть прозвища игроков из данных поставщика."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **opts):
        from parsers.sportmonks.client import SportmonksClient
        from parsers.sportmonks.importers import nickname_from
        from players.models import Player

        client = SportmonksClient()
        qs = Player.objects.exclude(sportmonks_id="").exclude(sportmonks_id__isnull=True)
        # Прозвища почти всегда у бразильцев и португальцев — составные фамилии или длинное ФИО.
        targets = [p for p in qs if len(f"{p.first_name} {p.last_name}".split()) >= 3]
        found = 0
        for p in targets:
            try:
                data = client.get_player(int(p.sportmonks_id))
            except Exception as e:  # сеть, лимит — пропускаем игрока
                self.stderr.write(f"{p.full_name}: {e}")
                continue
            nick = nickname_from(data or {}, f"{p.first_name} {p.last_name}".split())
            if nick:
                found += 1
                self.stdout.write(f"{p.full_name} → {nick}")
                if opts["apply"]:
                    Player.objects.filter(pk=p.pk).update(known_as=nick)
        self.stdout.write(self.style.SUCCESS(f"Проверено {len(targets)}, прозвищ: {found}{'' if opts['apply'] else ' (dry-run)'}"))
