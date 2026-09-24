# parsers/management/commands/fix_stale_player_teams.py
"""manage.py fix_stale_player_teams [--apply]

Разовая коррекция Player.team/number/position/last_match_at по самой свежей записи
MatchLineupPlayer (для всех игроков с историей составов, с sportmonks_id и без).
Игроков без составов не трогает. Без --apply — dry-run.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from lineups.models import MatchLineupPlayer
from players.models import Player


class Command(BaseCommand):
    help = "Чинит Player.team/number/position/last_match_at по самой свежей записи в MatchLineupPlayer (разовая коррекция бэкафилла не по хронологии + первичное проставление last_match_at всем игрокам)"

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Реально записать изменения (по умолчанию — только отчёт)")

    def handle(self, *args, **options):
        apply_changes = options["apply"]
        mode = "ПРИМЕНИТЬ" if apply_changes else "ТОЛЬКО ОТЧЁТ (dry-run, --apply чтобы применить)"
        self.stdout.write(self.style.WARNING(f"Режим: {mode}"))

        # Без фильтра по sportmonks_id.
        players = Player.objects.all()
        checked = fixed = no_lineup_data = already_correct = 0

        for player in players.iterator():
            checked += 1
            latest = (
                MatchLineupPlayer.objects
                .filter(player=player, lineup__match__start_time__isnull=False)
                .select_related("lineup__team", "lineup__match")
                .order_by("-lineup__match__start_time")
                .first()
            )
            if latest is None:
                no_lineup_data += 1
                continue

            latest_team = latest.lineup.team
            latest_at = latest.lineup.match.start_time
            latest_number = latest.shirt_number
            latest_position = latest.position

            needs_fix = (
                player.team_id != latest_team.id
                or player.last_match_at is None
                or player.last_match_at < latest_at
            )
            if not needs_fix:
                already_correct += 1
                continue

            self.stdout.write(
                f"  {player.full_name} (id={player.id}): "
                f"team {player.team} -> {latest_team}, "
                f"last_match_at {player.last_match_at} -> {latest_at} "
                f"(матч {latest.lineup.match_id})"
            )
            fixed += 1
            if apply_changes:
                player.team = latest_team
                player.last_match_at = latest_at
                if latest_number is not None:
                    player.number = latest_number
                if latest_position:
                    player.position = latest_position
                player.save(update_fields=["team", "last_match_at", "number", "position", "updated_at"])

        self.stdout.write(self.style.SUCCESS(
            f"Проверено {checked}: исправлено {fixed}, уже корректно {already_correct}, "
            f"без данных в MatchLineupPlayer (не тронуты) {no_lineup_data}"
        ))
