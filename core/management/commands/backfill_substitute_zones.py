# core/management/commands/backfill_substitute_zones.py
"""
manage.py backfill_substitute_zones [--apply] [--match-id ID]

2026-09-21, продолжение жалобы пользователя ("некоторые игроки стоят
например на правом полузащитнике, а сам игрок не играет там вообще") —
теперь для УЖЕ импортированных вышедших на замену игроков.

parsers/sportmonks/importers.py::import_events теперь наследует зону поля
(field_position, L/C/R) для вошедшего на замену от игрока, которого он
заменил (см. докстринг в import_events, ветка event_type == "substitution") —
но это применяется только на будущих импортах/пересинках. Все уже
сохранённые MatchLineupPlayer-строки для замен, у которых field_position
пустой, так и останутся пустыми без разовой чистки — эта команда её делает.

Источник данных: уже сохранённые MatchEvent с event_type="substitution" —
там уже есть и player (вошедший), и player_out (вышедший), сохранять
или перезапрашивать у Sportmonks заново ничего не нужно.

Как и dedupe_match_events.py/dedupe_referees_coaches.py — по умолчанию
ТОЛЬКО отчёт, --apply реально записывает изменения. После применения имеет
смысл прогнать recompute_closed_rounds (и/или дождаться следующего
планового пересчёта сезонной сборной) — сама эта команда только чинит
field_position, к пересчёту сборных не притрагивается.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from events.models import MatchEvent
from lineups.models import MatchLineupPlayer


class Command(BaseCommand):
    help = "Backfill: наследует field_position для замен от игрока, которого заменили (см. докстринг файла)"

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Реально записать изменения. Без флага — только отчёт.")
        parser.add_argument("--match-id", type=str, default=None, help="Ограничиться одним матчем (UUID)")

    def handle(self, *args, **options):
        apply_changes = options["apply"]
        match_id = options["match_id"]

        qs = MatchEvent.objects.filter(event_type="substitution").exclude(
            player_id__isnull=True
        ).exclude(player_out_id__isnull=True).select_related("match")
        if match_id:
            qs = qs.filter(match_id=match_id)

        mode = "ПРИМЕНИТЬ (записываю)" if apply_changes else "ТОЛЬКО ОТЧЁТ (dry-run, --apply чтобы применить)"
        self.stdout.write(self.style.WARNING(f"Режим: {mode}\n"))

        fixed = skipped_already_set = skipped_no_source_zone = skipped_no_lineup_row = 0

        for evt in qs.order_by("match_id", "minute"):
            incoming_row = MatchLineupPlayer.objects.filter(
                lineup__match_id=evt.match_id, player_id=evt.player_id
            ).first()
            outgoing_row = MatchLineupPlayer.objects.filter(
                lineup__match_id=evt.match_id, player_id=evt.player_out_id
            ).first()

            if not incoming_row or not outgoing_row:
                skipped_no_lineup_row += 1
                continue
            if incoming_row.field_position:
                skipped_already_set += 1
                continue
            if not outgoing_row.field_position:
                skipped_no_source_zone += 1
                continue

            self.stdout.write(
                f"Матч {evt.match} ({evt.match_id}), {evt.display_minute}: "
                f"{incoming_row.player} <- зона '{outgoing_row.field_position}' от {outgoing_row.player}"
            )
            if apply_changes:
                incoming_row.field_position = outgoing_row.field_position
                incoming_row.save(update_fields=["field_position"])
            fixed += 1

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\nИтого: {'исправлено' if apply_changes else 'к исправлению'} {fixed}, "
            f"уже была зона: {skipped_already_set}, у вышедшего тоже нет зоны: {skipped_no_source_zone}, "
            f"нет строки состава: {skipped_no_lineup_row}"
        ))
