# parsers/management/commands/import_api_archive.py
"""manage.py import_api_archive --year 2026 [--fixture ID] [--apply]
   manage.py import_api_archive --replay 2026-10-10 [--fixture ID] [--speed 10] [--notify] [--apply]

Импорт из архива сырых ответов (archive_api) — без доступа к API:
  --year   — переимпорт матчей сезона (например, после исправления импорта);
  --replay — проигрывание записанного live-дня: снимки матча по порядку с паузами (--speed — ускорение).
На время проигрывания синк с API выключается, после — возвращается. Рассылок нет, если не задан --notify.
Без --apply — только список того, что будет импортировано.
"""
from __future__ import annotations

import contextlib
import time
from datetime import datetime

from django.core.cache import cache
from django.core.management.base import BaseCommand, CommandError

from core.bulk import quiet
from core.models import PlatformSetting
from leagues.models import League
from parsers.sportmonks import archive, importers
from seasons.models import Season

SYNC_FLAG = "sportmonks_sync_enabled"


@contextlib.contextmanager
def sync_paused():
    """Выключает синк с API (флаг платформы) и возвращает прежнее значение."""
    setting, created = PlatformSetting.objects.get_or_create(
        key=SYNC_FLAG, defaults={"value": "true", "value_type": PlatformSetting.TYPE_BOOL})
    previous = setting.value
    setting.value = "false"
    setting.save(update_fields=["value", "updated_at"])
    cache.delete(f"platform_setting:{SYNC_FLAG}")
    try:
        yield
    finally:
        if created:
            setting.delete()
        else:
            setting.value = previous
            setting.save(update_fields=["value", "updated_at"])
        cache.delete(f"platform_setting:{SYNC_FLAG}")


class Command(BaseCommand):
    help = "Импорт и проигрывание матчей из архива сырых ответов API"

    def add_arguments(self, parser):
        mode = parser.add_mutually_exclusive_group(required=True)
        mode.add_argument("--year", type=int, help="Переимпорт матчей сезона из fixtures/<год>.")
        mode.add_argument("--replay", help="Дата live-записи (ГГГГ-ММ-ДД) из live/<дата>.")
        parser.add_argument("--fixture", help="Только этот матч (id поставщика).")
        parser.add_argument("--speed", type=float, default=1.0, help="Ускорение проигрывания (10 — в 10 раз быстрее).")
        parser.add_argument("--notify", action="store_true", help="С рассылками подписчикам (по умолчанию без).")
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        if options["year"]:
            files = sorted(archive.root().joinpath("fixtures", str(options["year"])).glob("*.json.gz"))
            if options["fixture"]:
                files = [f for f in files if f.name.startswith(f"{options['fixture']}.")]
            self._import(files, season_year=options["year"], options=options)
        else:
            day = archive.root().joinpath("live", options["replay"])
            pattern = f"fixture_{options['fixture']}_*.json.gz" if options["fixture"] else "fixture_*.json.gz"
            files = sorted(day.glob(pattern), key=lambda f: f.name.rsplit("_", 1)[-1])
            self._import(files, season_year=None, options=options, replay=True)

    def _import(self, files, season_year, options, replay: bool = False):
        if not files:
            raise CommandError("В архиве нет подходящих файлов.")
        self.stdout.write(f"Файлов: {len(files)} ({files[0].name} … {files[-1].name})")
        if not options["apply"]:
            self.stdout.write(self.style.WARNING("dry-run: --apply чтобы импортировать"))
            return
        league = League.objects.filter(sportmonks_id__isnull=False).first()
        seasons = {s.sportmonks_id: s for s in Season.objects.filter(league=league)}
        pause = sync_paused() if replay else contextlib.nullcontext()
        mute = contextlib.nullcontext() if options["notify"] else quiet()
        previous_at = None
        with pause, mute:
            for path in files:
                data = archive.load(path)
                season = seasons.get(str(data.get("season_id")))
                if season is None:
                    self.stderr.write(f"  {path.name}: сезон {data.get('season_id')} не найден, пропуск")
                    continue
                if replay:
                    at = datetime.strptime(path.name.rsplit("_", 1)[-1][:6], "%H%M%S")
                    if previous_at is not None:
                        time.sleep(max(0.0, (at - previous_at).total_seconds() / options["speed"]))
                    previous_at = at
                match = importers.import_full_fixture(data, league=league, season=season)
                self.stdout.write(f"  {path.name}: {match} {match.status} {match.home_score}:{match.away_score}")
        self.stdout.write(self.style.SUCCESS("Готово."))
