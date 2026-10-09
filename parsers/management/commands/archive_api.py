# parsers/management/commands/archive_api.py
"""manage.py archive_api [--year 2026] [--what fixtures,standings,squads,teams,people] [--refresh]

Сохраняет сырые ответы API в data/api_archive (parsers/sportmonks/archive.py):
  fixtures/<сезон>/<id>  — матч с полным include импорта (как в live-догрузке);
  standings/<сезон>, squads/<сезон>/<команда>, teams/<id>, coaches/<id>, referees/<id>, league.
Уже сохранённое пропускается (кроме незавершённых матчей); --refresh — перекачать всё.
Переимпорт без API: import_api_archive.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db.models import Q

from coaches.models import Coach
from matches.models import Match
from parsers.sportmonks import archive, importers
from parsers.sportmonks.client import SportmonksAPIError, SportmonksClient
from referees.models import Referee
from seasons.models import Season
from teams.models import Team

HAS_SM = Q(sportmonks_id__isnull=False) & ~Q(sportmonks_id="")
WHAT = ("fixtures", "standings", "squads", "teams", "people")


class Command(BaseCommand):
    help = "Архив сырых ответов API: матчи, таблицы, составы, команды, тренеры и судьи"

    def add_arguments(self, parser):
        parser.add_argument("--year", type=int, action="append", help="Сезон (можно несколько); по умолчанию все.")
        parser.add_argument("--what", default=",".join(WHAT), help=f"Через запятую из: {', '.join(WHAT)}")
        parser.add_argument("--refresh", action="store_true")

    def handle(self, *args, **options):
        self.client = SportmonksClient()
        self.refresh = options["refresh"]
        self.saved = self.skipped = self.failed = 0
        what = {w.strip() for w in options["what"].split(",") if w.strip()}
        seasons = Season.objects.exclude(sportmonks_id__isnull=True).exclude(sportmonks_id="").order_by("year")
        if options["year"]:
            seasons = seasons.filter(year__in=[str(y) for y in options["year"]])
        self._fetch(("league",), lambda: self.client.get_league(include="seasons"))
        for season in seasons:
            sid = int(season.sportmonks_id)
            self.stdout.write(self.style.MIGRATE_HEADING(f"Сезон {season.year} ({sid})"))
            if "fixtures" in what:
                matches = Match.objects.filter(season=season).exclude(sportmonks_id__isnull=True).only("sportmonks_id", "status")
                for m in matches:
                    self._fetch(("fixtures", season.year, m.sportmonks_id),
                                lambda m=m: self.client.get_fixture(int(m.sportmonks_id), include=importers.HEAVY_FIXTURE_INCLUDE),
                                force=m.status != "finished")
            if "standings" in what:
                self._fetch(("standings", season.year), lambda: self.client.get_standings(sid), force=True)
            if "squads" in what:
                for team in Team.objects.filter(Q(home_matches__season=season) | Q(away_matches__season=season)).filter(HAS_SM).distinct():
                    self._fetch(("squads", season.year, team.sportmonks_id),
                                lambda team=team: self.client.get_squad(sid, int(team.sportmonks_id)))
            self._report()
        if "teams" in what:
            for team in Team.objects.filter(HAS_SM):
                self._fetch(("teams", team.sportmonks_id),
                            lambda team=team: self.client.get_team(int(team.sportmonks_id), include="venue;sidelined.player"))
        if "people" in what:
            for coach in Coach.objects.filter(HAS_SM):
                self._fetch(("coaches", coach.sportmonks_id), lambda c=coach: self.client.get_coach(int(c.sportmonks_id)))
            for ref in Referee.objects.filter(HAS_SM):
                self._fetch(("referees", ref.sportmonks_id), lambda r=ref: self.client.get_referee(int(r.sportmonks_id)))
        self._report()
        self.stdout.write(f"Архив: {archive.root()}")

    def _fetch(self, parts, call, force: bool = False):
        parts = tuple(str(p) for p in parts)
        if not (self.refresh or force) and archive.path_for(*parts).exists():
            self.skipped += 1
            return
        try:
            archive.save(call(), *parts)
            self.saved += 1
        except (SportmonksAPIError, ValueError) as exc:
            self.failed += 1
            self.stderr.write(f"  {'/'.join(parts)}: {exc}")

    def _report(self):
        self.stdout.write(f"  сохранено {self.saved}, уже было {self.skipped}, ошибок {self.failed}")
