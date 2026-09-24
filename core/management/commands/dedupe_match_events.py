# core/management/commands/dedupe_match_events.py
"""manage.py dedupe_match_events [--apply] [--match-id ID]

Удаляет дубли MatchEvent с одинаковым (match, sportmonks_id):
оставляет запись с игроком, пустые заглушки удаляет. Неоднозначные группы — только в отчёт.
Без --apply — dry-run.
"""
from __future__ import annotations

from collections import defaultdict

from django.core.management.base import BaseCommand, CommandError

from events.models import MatchEvent


def _is_informative(event: MatchEvent) -> bool:
    """Есть ли у события игрок (player_id или имя в extra_data)."""
    if event.player_id:
        return True
    return bool((event.extra_data or {}).get("player_name"))


class Command(BaseCommand):
    help = "Находит/удаляет дубли MatchEvent с одинаковым sportmonks_id (см. докстринг файла)"

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Реально удалить дубли. Без флага — только отчёт.")
        parser.add_argument("--match-id", type=str, default=None, help="Ограничиться одним матчем (UUID)")

    def handle(self, *args, **options):
        apply_changes = options["apply"]
        match_id = options["match_id"]

        qs = MatchEvent.objects.exclude(sportmonks_id__isnull=True).select_related("match")
        if match_id:
            qs = qs.filter(match_id=match_id)

        groups: dict[tuple, list[MatchEvent]] = defaultdict(list)
        for event in qs:
            groups[(event.match_id, event.sportmonks_id)].append(event)

        duplicate_groups = {k: v for k, v in groups.items() if len(v) > 1}
        if not duplicate_groups:
            self.stdout.write(self.style.SUCCESS("Дублей не найдено."))
            return

        mode = "ПРИМЕНИТЬ (удаляю)" if apply_changes else "ТОЛЬКО ОТЧЁТ (dry-run, --apply чтобы применить)"
        self.stdout.write(self.style.WARNING(f"Режим: {mode}\n"))

        deleted_total = 0
        unresolved = 0
        for (match_id_, sm_id), events in duplicate_groups.items():
            match = events[0].match
            informative = [e for e in events if _is_informative(e)]
            blank = [e for e in events if not _is_informative(e)]

            self.stdout.write(
                f"Матч {match} (id={match_id_}), sportmonks_id события={sm_id}: "
                f"{len(events)} строк(и) в базе"
            )
            for e in events:
                marker = "с данными" if _is_informative(e) else "ПУСТАЯ"
                self.stdout.write(f"    id={e.id}, минута={e.display_minute}, {marker}")

            if len(informative) == 1 and blank:
                # Одна содержательная запись, остальные пустые — удаляем пустые.
                to_delete = blank
                self.stdout.write(self.style.SUCCESS(
                    f"    -> оставляю id={informative[0].id} (минута {informative[0].display_minute}), "
                    f"удаляю {len(to_delete)} пустую(ые)"
                ))
                if apply_changes:
                    for e in to_delete:
                        e.delete()
                deleted_total += len(to_delete)
            else:
                # Неоднозначно — не удаляем.
                unresolved += 1
                self.stdout.write(self.style.ERROR(
                    "    -> НЕОДНОЗНАЧНО (не 1 содержательная запись из группы) — пропущено, разберитесь вручную"
                ))
            self.stdout.write("")

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"Итого: {'удалено' if apply_changes else 'к удалению'} {deleted_total} дубликатов, "
            f"неоднозначных групп (пропущено): {unresolved}"
        ))
