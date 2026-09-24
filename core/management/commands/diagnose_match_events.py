# core/management/commands/diagnose_match_events.py
"""manage.py diagnose_match_events "Кайрат" "Тобол" [--year 2026]

Read-only: печатает сырой extra_data всех событий найденного матча.
"""
from __future__ import annotations

import difflib
import json

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from core.utils import normalize_kz
from events.models import MatchEvent
from matches.models import Match
from teams.models import Team

# Порог нечёткого совпадения названий команд (как в dedupe_referees_coaches --fuzzy).
FUZZY_THRESHOLD = 0.8


class Command(BaseCommand):
    help = "Печатает сырой extra_data всех событий матча (диагностика, ничего не меняет)"

    def add_arguments(self, parser):
        parser.add_argument("teams", nargs="+", help="Слова из названий команд, например: Кайрат Тобыл")
        parser.add_argument("--year", type=int, default=None, help="Год матча (по умолчанию — любой)")
        parser.add_argument("--limit", type=int, default=5, help="Сколько последних подходящих матчей показать")

    def handle(self, *args, **options):
        words = options["teams"]
        year = options["year"]
        limit = options["limit"]

        team_ids = []
        all_teams = list(Team.objects.only("id", "name"))
        for word in words:
            normalized = normalize_kz(word)
            # Сначала точное вхождение, потом нечёткое.
            exact = [t.id for t in all_teams if normalized in normalize_kz(t.name)]
            if exact:
                team_ids.extend(exact)
                continue
            fuzzy = [
                t for t in all_teams
                if difflib.SequenceMatcher(None, normalized, normalize_kz(t.name)).ratio() >= FUZZY_THRESHOLD
            ]
            if fuzzy:
                self.stdout.write(self.style.WARNING(
                    f"'{word}' не совпало точно ни с одной командой — беру ближайшее по написанию: "
                    + ", ".join(t.name for t in fuzzy)
                ))
                team_ids.extend(t.id for t in fuzzy)

        if not team_ids:
            raise CommandError(f"Ни одна команда не найдена по словам {words!r} (даже нечётким совпадением)")

        # Сначала завершённые матчи, среди них — новые первыми.
        qs = Match.objects.filter(
            Q(home_team_id__in=team_ids) | Q(away_team_id__in=team_ids)
        ).select_related("home_team", "away_team").order_by("-status", "-start_time")
        # Приоритет статуса задаём явно.
        qs = sorted(qs, key=lambda m: (m.status != "finished", -m.start_time.timestamp()))
        if year:
            qs = [m for m in qs if m.start_time.year == year]

        matches = qs[:limit]
        if not matches:
            raise CommandError("Матчи не найдены")

        for match in matches:
            self.stdout.write(self.style.MIGRATE_HEADING(
                f"\n{match.home_team.name} {match.home_score}:{match.away_score} {match.away_team.name} "
                f"({match.start_time:%d.%m.%Y}, id={match.id}, sportmonks_id={match.sportmonks_id})"
            ))
            events = MatchEvent.objects.filter(match=match).order_by("minute", "added_time", "id")
            if not events.exists():
                self.stdout.write("  (событий нет)")
                continue
            for event in events:
                blank = not event.player_id and not (event.extra_data or {}).get("player_name")
                marker = self.style.ERROR("  ПУСТОЕ") if blank and event.event_type == "goal" else ""
                self.stdout.write(
                    f"  {event.display_minute}' {event.get_event_type_display()} "
                    f"[{event.team_side}] player_id={event.player_id} "
                    f"sportmonks_id={event.sportmonks_id}{marker}"
                )
                self.stdout.write(f"    extra_data: {json.dumps(event.extra_data, ensure_ascii=False)}")
