# players/management/commands/merge_safe_duplicates.py
"""manage.py merge_safe_duplicates [--apply]

Сливает дубли игроков, которые точно один человек: одинаковое ФИО, один клуб (или у одной записи нет клуба
или id поставщика) и ни разу не стояли в одной заявке. Остальные одноимённые — в отчёт на ручной разбор.
Оставляем запись с id поставщика и большим числом матчей; id второй становится алиасом (players.services).
Без --apply — dry-run.
"""
from __future__ import annotations

from collections import defaultdict
from itertools import combinations

from django.core.management.base import BaseCommand
from django.db.models import Count

from core.utils import normalize_kz
from lineups.models import MatchLineupPlayer
from players.models import Player
from players.services import merge_players


def _added_by_event(row) -> bool:
    """Строка, дописанная по событию (importers._add_players_from_events): без номера и позиции."""
    return not row.is_starting and row.shirt_number is None and not row.position


def _same_person(a: Player, b: Player) -> bool:
    shared = set(MatchLineupPlayer.objects.filter(player=a).values_list("lineup__match_id", flat=True)) & set(
        MatchLineupPlayer.objects.filter(player=b).values_list("lineup__match_id", flat=True))
    for match_id in shared:
        rows = MatchLineupPlayer.objects.filter(lineup__match_id=match_id, player__in=[a, b])
        if not any(_added_by_event(r) for r in rows):
            return False  # оба честно в одной заявке — разные люди
    same_team = a.team_id and a.team_id == b.team_id
    return bool(same_team or not a.team_id or not b.team_id or not a.sportmonks_id or not b.sportmonks_id)


class Command(BaseCommand):
    help = "Сливает гарантированные дубли игроков; спорные — в отчёт"

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        groups = defaultdict(list)
        players = Player.objects.annotate(n=Count("matchlineupplayer")).only("id", "first_name", "last_name",
                                                                             "team_id", "sportmonks_id")
        for p in players:
            groups[(normalize_kz(p.first_name.strip()), normalize_kz(p.last_name.strip()))].append(p)
        merged = manual = 0
        for (_first, _last), members in groups.items():
            if len(members) < 2:
                continue
            # Основная запись: с id поставщика и наибольшим числом матчей.
            members.sort(key=lambda p: (bool(p.sportmonks_id), p.n), reverse=True)
            keep, rest = members[0], members[1:]
            for other in rest:
                if _same_person(keep, other):
                    merged += 1
                    self.stdout.write(f"  СЛИТЬ: {other.full_name} ({other.sportmonks_id or 'без id'}, матчей {other.n})"
                                      f" → {keep.full_name} ({keep.sportmonks_id or 'без id'}, матчей {keep.n})")
                    if options["apply"]:
                        merge_players(keep, Player.objects.get(pk=other.pk), apply=True)
                else:
                    manual += 1
                    self.stdout.write(self.style.WARNING(
                        f"  РУЧНОЙ РАЗБОР: {other.full_name} — {other.team or 'без клуба'} / {keep.team or 'без клуба'} "
                        f"(id {other.sportmonks_id} и {keep.sportmonks_id})"))
        mode = "слито" if options["apply"] else "будет слито (dry-run)"
        self.stdout.write(self.style.SUCCESS(f"{mode}: {merged}, на ручной разбор: {manual}"))
