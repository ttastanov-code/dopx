# parsers/management/commands/sportmonks_backfill_stats.py
"""
Догрузка ПОЛНОЙ статистики (весь raw: рейтинг Sportmonks, отборы,
перехваты, выносы и т.д.) для уже сыгранных матчей — 2026-09-24.

До этого дня импорт выбрасывал всё, кроме ~10 полей игрока и ~15 полей
команды, поэтому у прошлых матчей в raw нет RATING/TACKLES/INTERCEPTIONS.
Без них детектор расхождения (aggregates/tasks.py::
_check_player_stats_divergence) не может сравнить защитника с его же нормой.

Что делает: для каждого завершённого матча с sportmonks_id — 1 лёгкий
запрос (только statistics + lineups.details, без событий/составов) и
перезапись MatchTeamStatistics/MatchPlayerStatistics. Составы, события,
счёт НЕ трогает.

Расход лимита: 1 запрос на матч. Лимит Sportmonks — ~3000 запросов в час
на сущность, сезон КПЛ — ~180 матчей, укладывается с запасом.

Использование:
    python manage.py sportmonks_backfill_stats              # все завершённые матчи без полного raw
    python manage.py sportmonks_backfill_stats --season-only # только текущий сезон
    python manage.py sportmonks_backfill_stats --force       # перезалить и те, где raw уже полный
    python manage.py sportmonks_backfill_stats --limit 20    # первые 20 (для пробы)
"""
import time

from django.core.management.base import BaseCommand

from matches.models import Match, MatchPlayerStatistics
from parsers.sportmonks.client import SportmonksAPIError, SportmonksClient
from parsers.sportmonks.importers import import_player_statistics, import_statistics

LIGHT_STATS_INCLUDE = "participants;statistics.type;lineups.details.type"


class Command(BaseCommand):
    help = "Догрузить полную статистику Sportmonks (рейтинг, отборы, перехваты...) для прошлых матчей"

    def add_arguments(self, parser):
        parser.add_argument("--season-only", action="store_true", help="Только текущий сезон")
        parser.add_argument("--force", action="store_true", help="Перезалить и матчи, где полный raw уже есть")
        parser.add_argument("--limit", type=int, default=0, help="Обработать не больше N матчей")
        parser.add_argument("--sleep", type=float, default=0.3, help="Пауза между запросами, сек")

    def handle(self, *args, **options):
        qs = (
            Match.objects.filter(status="finished", sportmonks_id__isnull=False)
            .exclude(sportmonks_id="")
            .select_related("home_team", "away_team")
            .order_by("-start_time")
        )
        if options["season_only"]:
            from seasons.models import Season
            current = Season.get_primary_active()
            if current is None:
                self.stderr.write(self.style.ERROR("Не найден активный сезон (Season.get_primary_active)"))
                return
            qs = qs.filter(season=current)

        matches = list(qs)
        if not options["force"]:
            # "Полный raw" = у матча есть статистика хотя бы одного игрока с
            # ключом RATING/MINUTES_PLAYED (старый импорт такие не сохранял).
            done_ids = set(
                MatchPlayerStatistics.objects.filter(match__in=matches)
                .filter(raw__has_key="MINUTES_PLAYED")
                .values_list("match_id", flat=True)
            )
            matches = [m for m in matches if m.id not in done_ids]
        if options["limit"]:
            matches = matches[: options["limit"]]

        total = len(matches)
        self.stdout.write(f"Матчей к догрузке: {total} (1 запрос на матч)")
        if not total:
            return

        client = SportmonksClient()
        ok = empty = errors = 0
        for i, match in enumerate(matches, 1):
            try:
                data = client.get_fixture(int(match.sportmonks_id), include=LIGHT_STATS_INCLUDE)
            except SportmonksAPIError as exc:
                errors += 1
                self.stderr.write(f"[{i}/{total}] {match}: ошибка API — {exc}")
                if "429" in str(exc) or "limit" in str(exc).lower():
                    self.stderr.write(self.style.WARNING("Похоже на лимит запросов — останавливаюсь, запустите позже."))
                    break
                continue

            team_ok = import_statistics(match, data.get("statistics") or [])
            player_ok = import_player_statistics(match, data.get("lineups") or [])
            if team_ok or player_ok:
                ok += 1
            else:
                empty += 1
            if i % 10 == 0 or i == total:
                self.stdout.write(f"[{i}/{total}] обработано, с данными: {ok}, без статистики у Sportmonks: {empty}, ошибок: {errors}")
            time.sleep(options["sleep"])

        self.stdout.write(self.style.SUCCESS(
            f"Готово: с данными {ok}, без статистики {empty}, ошибок {errors}. "
            f"Детектор расхождения подхватит новые данные при следующем ежедневном прогоне."
        ))
