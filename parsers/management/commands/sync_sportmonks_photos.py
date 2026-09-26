# parsers/management/commands/sync_sportmonks_photos.py
"""manage.py sync_sportmonks_photos [--entity {players,referees,coaches,all}] [--season-id N] [--apply]

Догружает photo_url из Sportmonks: игроки — по составам команд текущего сезона
(запрос на команду), судьи и тренеры — поштучно по sportmonks_id.
Заглушки Sportmonks пропускаются. Без --apply — только отчёт о покрытии.
Дальше фото обновляются обычным синком матчей.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from parsers.sportmonks.client import SportmonksAPIError, SportmonksClient
from parsers.sportmonks.importers import sportmonks_photo_url


class Command(BaseCommand):
    help = "Догружает фото игроков, судей и тренеров из Sportmonks"

    def add_arguments(self, parser):
        parser.add_argument("--entity", choices=["players", "referees", "coaches", "all"], default="all")
        parser.add_argument("--season-id", type=int, default=None, help="Sportmonks season id (по умолчанию — текущий).")
        parser.add_argument("--apply", action="store_true", help="Записать photo_url.")

    def handle(self, *args, **options):
        self.client = SportmonksClient()
        self.apply_changes = options["apply"]
        self.stdout.write(self.style.WARNING("Режим: " + ("ПРИМЕНИТЬ" if self.apply_changes else "dry-run (--apply чтобы записать)")))
        entity = options["entity"]
        if entity in ("players", "all"):
            self._players(options["season_id"] or self._current_season_id())
        if entity in ("referees", "all"):
            from referees.models import Referee
            self._one_by_one("Судьи", Referee, self.client.get_referee)
        if entity in ("coaches", "all"):
            from coaches.models import Coach
            self._one_by_one("Тренеры", Coach, self.client.get_coach)

    def _current_season_id(self) -> int:
        season = self.client.get_league(include="currentSeason").get("currentseason") or {}
        if not season.get("id"):
            raise SystemExit("Не удалось определить текущий сезон — передайте --season-id.")
        return season["id"]

    def _save(self, obj, url: str) -> bool:
        if not url or obj.photo_url == url:
            return False
        if self.apply_changes:
            obj.photo_url = url
            obj.save(update_fields=["photo_url", "updated_at"])
        return True

    def _report(self, title: str, total: int, with_photo: int, changed: int, errors: int = 0):
        pct = round(with_photo / total * 100) if total else 0
        verb = "обновлено" if self.apply_changes else "будет обновлено"
        self.stdout.write(
            f"{title}: проверено {total}, с фото в Sportmonks {with_photo} ({pct}%), {verb} {changed}"
            + (f", ошибок {errors}" if errors else "")
        )

    def _players(self, season_id: int):
        from players.models import Player
        from teams.models import Team

        urls: dict[str, str] = {}
        errors = 0
        teams = Team.objects.exclude(sportmonks_id__isnull=True).exclude(sportmonks_id="")
        for team in teams:
            try:
                squad = self.client.get_squad(season_id, int(team.sportmonks_id), include="player")
            except SportmonksAPIError as exc:
                errors += 1
                self.stdout.write(self.style.ERROR(f"  {team.name}: {exc}"))
                continue
            for row in squad:
                player = row.get("player") or {}
                if player.get("id"):
                    urls[str(player["id"])] = sportmonks_photo_url(player)

        players = Player.objects.filter(sportmonks_id__in=urls.keys())
        changed = sum(self._save(p, urls[p.sportmonks_id]) for p in players)
        self._report("Игроки (составы сезона)", len(urls), sum(1 for u in urls.values() if u), changed, errors)

    def _one_by_one(self, title: str, model, fetch):
        people = model.objects.exclude(sportmonks_id__isnull=True).exclude(sportmonks_id="")
        with_photo = changed = errors = 0
        for person in people:
            try:
                url = sportmonks_photo_url(fetch(int(person.sportmonks_id)))
            except (SportmonksAPIError, ValueError) as exc:
                errors += 1
                self.stdout.write(self.style.ERROR(f"  {person}: {exc}"))
                continue
            with_photo += bool(url)
            changed += self._save(person, url)
        self._report(title, people.count(), with_photo, changed, errors)
