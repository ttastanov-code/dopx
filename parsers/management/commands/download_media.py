# parsers/management/commands/download_media.py
"""manage.py download_media [--entity {players,coaches,referees,teams,all}] [--refresh] [--apply]

Скачивает фото и гербы с CDN поставщика в своё хранилище (parsers/media.py): сайт не зависит от чужого CDN.
Заглушки поставщика не сохраняются, уже скачанные — удаляются. --refresh — перекачать всё. Без --apply — dry-run.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from parsers import media


class Command(BaseCommand):
    help = "Скачивает фото и гербы с CDN поставщика в своё хранилище"

    def add_arguments(self, parser):
        parser.add_argument("--entity", choices=[*media.entities(), "all"], default="all")
        parser.add_argument("--refresh", action="store_true", help="Перекачать и уже скачанные.")
        parser.add_argument("--workers", type=int, default=8)
        parser.add_argument("--apply", action="store_true", help="Записать файлы (по умолчанию — dry-run).")

    def handle(self, *args, **options):
        apply_changes = options["apply"]
        self.stdout.write(self.style.WARNING("Режим: " + ("ПРИМЕНИТЬ" if apply_changes else "dry-run (--apply чтобы записать)")))
        names = list(media.entities()) if options["entity"] == "all" else [options["entity"]]
        for name in names:
            count = media.pending(name, options["refresh"]).count()
            self.stdout.write(f"{name}: к скачиванию {count}")
            if apply_changes and count:
                stats = media.localize(name, options["refresh"], options["workers"])
                self.stdout.write(self.style.SUCCESS(
                    f"  скачано {stats['saved']}, заглушек {stats['placeholders']}, не вышло {stats['failed']}"))
        if apply_changes:
            self.stdout.write(self.style.SUCCESS(f"Удалено ранее скачанных заглушек: {media.purge_placeholders()}"))
