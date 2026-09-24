# parsers/management/commands/fix_known_wrong_names.py
"""manage.py fix_known_wrong_names [--apply]

Исправляет в базе известные ошибки кириллицы от Sportmonks (Player/Referee/Coach)
по словарю PLAYER_NAME_CORRECTIONS — тот же словарь применяется на импорте.
Без --apply — dry-run.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from coaches.models import Coach
from parsers.sportmonks.name_translations import PLAYER_NAME_CORRECTIONS
from players.models import Player
from referees.models import Referee

# Словарь: неверная кириллица (нижний регистр) -> верная.
KNOWN_WRONG_CYRILLIC = PLAYER_NAME_CORRECTIONS


class Command(BaseCommand):
    help = (
        "Чинит Player/Referee/Coach с известными неверными кириллическими именами "
        "от Sportmonks (см. parsers/sportmonks/name_translations.py::PLAYER_NAME_CORRECTIONS)"
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Реально записать изменения (по умолчанию — только отчёт)")

    def handle(self, *args, **options):
        apply_changes = options["apply"]
        mode = "ПРИМЕНИТЬ" if apply_changes else "ТОЛЬКО ОТЧЁТ (dry-run, --apply чтобы применить)"
        self.stdout.write(self.style.WARNING(f"Режим: {mode}"))

        total_fixed = 0
        for model, label in ((Player, "игрок"), (Referee, "судья"), (Coach, "тренер")):
            total_fixed += self._fix_model(model, label, apply_changes)

        suffix = " (--apply чтобы применить)" if not apply_changes else ""
        self.stdout.write(self.style.SUCCESS(
            f"Итого исправлено записей: {total_fixed}{suffix}"
        ))

    def _fix_model(self, model, label: str, apply_changes: bool) -> int:
        fixed = 0
        for obj in model.objects.all():
            update_fields = []
            old_first, old_last = obj.first_name, obj.last_name

            # Сравнение в нижнем регистре.
            new_first = KNOWN_WRONG_CYRILLIC.get((obj.first_name or "").strip().lower())
            if new_first is not None and new_first != obj.first_name:
                obj.first_name = new_first
                update_fields.append("first_name")

            new_last = KNOWN_WRONG_CYRILLIC.get((obj.last_name or "").strip().lower())
            if new_last is not None and new_last != obj.last_name:
                obj.last_name = new_last
                update_fields.append("last_name")

            if not update_fields:
                continue

            self.stdout.write(
                f"  {label} id={obj.id} (sportmonks_id={obj.sportmonks_id}): "
                f"{old_first!r} {old_last!r} -> {obj.first_name!r} {obj.last_name!r}"
            )
            fixed += 1
            if apply_changes:
                obj.save(update_fields=update_fields + ["updated_at"])

        return fixed
