# parsers/management/commands/sportmonks_inspect_stats.py
"""manage.py sportmonks_inspect_stats [--fixture ID]

Один запрос по завершённому матчу: печатает все типы статистики игроков (по амплуа)
и команд, сохраняет JSON в sportmonks_samples/fixture_<id>.json. В БД не пишет.
"""
import json
from collections import defaultdict
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from matches.models import Match
from parsers.sportmonks.client import SportmonksAPIError, SportmonksClient
from parsers.sportmonks.importers import (
    HEAVY_FIXTURE_INCLUDE,
    PLAYER_STAT_DEV_NAME_MAP,
    POSITION_ID_MAP,
    TEAM_STAT_DEV_NAME_MAP,
)


class Command(BaseCommand):
    help = "Показать ВСЕ типы статистики, которые Sportmonks отдаёт по матчу (1 запрос, в БД ничего не пишет)"

    def add_arguments(self, parser):
        parser.add_argument("--fixture", type=int, help="Sportmonks ID матча (по умолчанию — последний завершённый из БД)")

    def handle(self, *args, **options):
        self.stdout.write(f"Токен: {getattr(settings, 'SPORTMONKS_API_TOKEN_SOURCE', 'MAIN')}")

        fixture_id = options.get("fixture")
        if not fixture_id:
            match = (
                Match.objects.filter(status="finished", sportmonks_id__isnull=False)
                .exclude(sportmonks_id="")
                .order_by("-start_time")
                .first()
            )
            if match is None:
                self.stderr.write(self.style.ERROR("В БД нет завершённых матчей с sportmonks_id — укажите --fixture"))
                return
            fixture_id = int(match.sportmonks_id)
            self.stdout.write(f"Матч: {match} (sportmonks_id={fixture_id})")

        try:
            data = SportmonksClient().get_fixture(fixture_id, include=HEAVY_FIXTURE_INCLUDE)
        except SportmonksAPIError as exc:
            self.stderr.write(self.style.ERROR(f"Запрос не прошёл: {exc}"))
            self.stderr.write(self.style.WARNING("Проверьте токен в .env и что подписка/трайл активны."))
            return

        out_dir = Path(settings.BASE_DIR) / "sportmonks_samples"
        out_dir.mkdir(exist_ok=True)
        out_path = out_dir / f"fixture_{fixture_id}.json"
        out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        self.stdout.write(self.style.SUCCESS(f"Полный ответ сохранён: {out_path}"))

        # --- игроки ---
        lineups = data.get("lineups") or []
        players_by_pos = defaultdict(int)
        types_by_pos = defaultdict(lambda: defaultdict(int))
        examples = {}
        players_with_details = 0
        for entry in lineups:
            pos = POSITION_ID_MAP.get(entry.get("position_id"), "?")
            players_by_pos[pos] += 1
            details = entry.get("details") or []
            if details:
                players_with_details += 1
            for d in details:
                name = (d.get("type") or {}).get("developer_name") or f"type_id={d.get('type_id')}"
                types_by_pos[name][pos] += 1
                examples.setdefault(name, (d.get("data") or {}).get("value"))

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"СТАТИСТИКА ИГРОКОВ: {len(lineups)} в составе, у {players_with_details} есть details"
        ))
        pos_order = ["GK", "D", "M", "F", "?"]
        header = "  ".join(f"{p}({players_by_pos.get(p, 0)})".rjust(7) for p in pos_order)
        self.stdout.write(f"{'тип (developer_name)':<32}{header}   пример   [уже используем]")
        for name in sorted(types_by_pos):
            counts = "  ".join(str(types_by_pos[name].get(p, 0)).rjust(7) for p in pos_order)
            used = "✓" if name in PLAYER_STAT_DEV_NAME_MAP else ""
            self.stdout.write(f"{name:<32}{counts}   {str(examples.get(name))[:8]:<8} {used}")
        if not types_by_pos:
            self.stdout.write(self.style.WARNING("  Ни у одного игрока нет details — статистика игроков по этому матчу не пришла."))

        # --- команды ---
        team_types = {}
        for s in data.get("statistics") or []:
            name = (s.get("type") or {}).get("developer_name") or f"type_id={s.get('type_id')}"
            team_types.setdefault(name, (s.get("data") or {}).get("value"))
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING(f"СТАТИСТИКА КОМАНД: {len(team_types)} типов"))
        for name in sorted(team_types):
            used = "✓" if name in TEAM_STAT_DEV_NAME_MAP else ""
            self.stdout.write(f"  {name:<32} {str(team_types[name])[:10]:<10} {used}")

        defensive = [n for n in types_by_pos if any(k in n for k in ("TACKLE", "INTERCEPT", "CLEARANCE", "DUEL", "BLOCK", "RECOVER"))]
        self.stdout.write("")
        if defensive:
            self.stdout.write(self.style.SUCCESS("Защитные метрики у игроков ЕСТЬ: " + ", ".join(sorted(defensive))))
        else:
            self.stdout.write(self.style.WARNING("Защитных метрик (отборы/перехваты/выносы/единоборства) у игроков НЕТ."))
