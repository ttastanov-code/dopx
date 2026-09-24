# players/management/commands/backfill_player_positions.py
"""manage.py backfill_player_positions [--apply]

Заполняет пустой Player.position модой позиции из MatchLineupPlayer.
Уже заполненные не трогает. Без --apply — dry-run.
"""
from __future__ import annotations

from collections import Counter, defaultdict

from django.core.management.base import BaseCommand

from lineups.models import MatchLineupPlayer
from players.models import Player
from players.positions import clean_position_code


class Command(BaseCommand):
    help = "Бэкафиллит пустую Player.position по истории составов матчей (без внешних сайтов)"

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Реально записать позицию")

    def handle(self, *args, **options):
        apply_changes: bool = options["apply"]

        players_without_position = list(Player.objects.filter(position=""))
        if not players_without_position:
            self.stdout.write(self.style.SUCCESS("У всех игроков уже проставлена позиция — нечего бэкафиллить."))
            return

        player_ids = [p.id for p in players_without_position]
        rows = (
            MatchLineupPlayer.objects
            .filter(player_id__in=player_ids)
            .exclude(position="")
            .values_list("player_id", "position")
        )
        counters: dict[str, Counter] = defaultdict(Counter)
        for player_id, position in rows:
            counters[str(player_id)][clean_position_code(position)] += 1

        to_update: list[tuple[Player, str]] = []
        still_unknown = []
        for player in players_without_position:
            counter = counters.get(str(player.id))
            if not counter:
                still_unknown.append(player)
                continue
            code, _n = counter.most_common(1)[0]
            to_update.append((player, code))

        self.stdout.write(
            f"Всего без позиции: {len(players_without_position)}. "
            f"Можно восстановить по истории составов: {len(to_update)}. "
            f"НЕТ данных вообще (ни разу не было в составе с амплуа) — вручную через админку: {len(still_unknown)}."
        )

        if not apply_changes:
            self.stdout.write(self.style.NOTICE("dry-run — ничего не записано. Примеры (первые 20):"))
            for player, code in to_update[:20]:
                self.stdout.write(f"  · {player.full_name} → {code}")
            if len(to_update) > 20:
                self.stdout.write(f"  … и ещё {len(to_update) - 20}")
            return

        updated = 0
        for player, code in to_update:
            player.position = code
            player.save(update_fields=["position"])
            updated += 1

        self.stdout.write(self.style.SUCCESS(f"Готово — проставлена позиция {updated} игрокам."))
        if still_unknown:
            self.stdout.write(self.style.WARNING(
                f"{len(still_unknown)} игроков так и остались без позиции — в истории составов матчей "
                f"позиция ни разу не заполнена, восстановить неоткуда, только вручную в админке."
            ))
