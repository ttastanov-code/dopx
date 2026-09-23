# players/management/commands/merge_duplicate_players.py
"""
manage.py merge_duplicate_players --keep <uuid> --merge <uuid> [--apply]

2026-09-22, прямая просьба пользователя после diagnose_duplicate_players.
Тонкая CLI-обёртка над players/services.py::merge_players — сама логика
переноса связей (составы/события/оценки/агрегаты/подписки, включая
построчный перенос для связей с UniqueConstraint на игрока) живёт там же,
её же использует dashboard/views.py::duplicate_players_merge — очередь
«Дубли игроков» на дашборде, где слияние делается одной кнопкой прямо из
флага PotentialDuplicatePlayer, без ручного ввода id (2026-09-22, прямая
просьба пользователя: "пздц это муторно копировать, вставлять... надо
оптимизировать"). Эта команда — для разового ручного разбора через
терминал, когда id уже известны и очередь на дашборде не нужна.

Без --apply — только отчёт (что будет перенесено/сколько конфликтов),
ничего не меняется.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from players.models import Player
from players.services import merge_players


class Command(BaseCommand):
    help = (
        "Объединяет дубль игрока (см. diagnose_duplicate_players): переносит статистику "
        "с --merge на --keep, затем удаляет --merge. Без --apply — только отчёт."
    )

    def add_arguments(self, parser):
        parser.add_argument("--keep", required=True, help="UUID записи, которую оставляем.")
        parser.add_argument("--merge", required=True, help="UUID записи, которую сливаем и УДАЛЯЕМ после переноса данных.")
        parser.add_argument("--apply", action="store_true", help="Реально выполнить. Без флага — только отчёт, ничего не меняется.")

    def handle(self, *args, **options):
        try:
            keep = Player.objects.get(id=options["keep"])
        except Player.DoesNotExist:
            raise CommandError(f"--keep={options['keep']}: такого игрока нет.")
        try:
            merge = Player.objects.get(id=options["merge"])
        except Player.DoesNotExist:
            raise CommandError(f"--merge={options['merge']}: такого игрока нет.")
        if keep.id == merge.id:
            raise CommandError("--keep и --merge — один и тот же id.")

        apply = options["apply"]
        self.stdout.write(f"=== Слияние: {merge.full_name} → {keep.full_name} ===")
        if not apply:
            self.stdout.write(self.style.WARNING("Режим dry-run — ничего не меняется. Добавьте --apply, чтобы выполнить реально."))

        report = merge_players(keep, merge, apply=apply)
        for line in report.lines:
            self.stdout.write(f"  {line}")

        if apply:
            self.stdout.write(self.style.SUCCESS("Готово."))
        else:
            self.stdout.write(self.style.WARNING("Dry-run завершён — ничего не изменено. Повторите с --apply, чтобы выполнить реально."))
