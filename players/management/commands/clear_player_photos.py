# players/management/commands/clear_player_photos.py
"""manage.py clear_player_photos [--apply]

Удаляет фото игроков, оставшиеся от старого импорта с kffleague.kz
(файл + поле). Coach.photo не трогает. Без --apply — dry-run.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from players.models import Player


class Command(BaseCommand):
    help = "Удаляет фото игроков (файл + поле Player.photo), оставшиеся от отменённого импорта с KFF"

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Реально удалить файлы и очистить поле")

    def handle(self, *args, **options):
        apply_changes: bool = options["apply"]

        players_with_photo = Player.objects.exclude(photo="").exclude(photo__isnull=True)
        count = players_with_photo.count()

        if count == 0:
            self.stdout.write(self.style.SUCCESS("У игроков нет проставленных фото — нечего чистить."))
            return

        if not apply_changes:
            self.stdout.write(self.style.NOTICE(
                f"dry-run: у {count} игроков проставлено фото. Запустите с --apply, чтобы удалить файлы "
                f"и очистить поле (на сайте вместо них появится генеративный аватар)."
            ))
            for player in players_with_photo[:20]:
                self.stdout.write(f"  · {player.full_name} ({player.team.name if player.team else '—'})")
            if count > 20:
                self.stdout.write(f"  … и ещё {count - 20}")
            return

        cleared = 0
        for player in players_with_photo.iterator():
            # Файл удаляем без save, поле очищаем одним save ниже.
            player.photo.delete(save=False)
            player.photo = None
            player.save(update_fields=["photo"])
            cleared += 1

        self.stdout.write(self.style.SUCCESS(f"Готово — очищено фото у {cleared} игроков."))
