# parsers/management/commands/sync_sportmonks_season.py
"""
Бэкафилл истории КПЛ из Sportmonks (фаза 3, docs/sportmonks-migration-plan.md).

База пустая (manage.py flush перед началом работ) — пользователь решил
затянуть все 3 доступных на Starter-плане без доп. оплаты сезона: 2024,
2025, 2026 (id сезонов читаются динамически из /leagues/393?include=seasons,
НЕ хардкодятся — состав сезонов на стороне Sportmonks может отличаться от
предположений).

Использование:
    python manage.py sync_sportmonks_season                      # все сезоны
    python manage.py sync_sportmonks_season --year 2026          # один сезон
    python manage.py sync_sportmonks_season --year 2026 --limit 5  # тест на 5 матчах

РЕКОМЕНДАЦИЯ (см. план, фаза 3 — "тестировать на 5-6 уже сыгранных матчах
текущего сезона плюс 1-2 матчах сезона 2024/2025, сверяя руками с тем, что
показывает сайт Sportmonks и текущая версия страницы матча на DOPX"):
    python manage.py sync_sportmonks_season --year 2026 --limit 6
и только после ручной проверки результата на странице матча — полный прогон
без --limit по всем трём сезонам.

ВАЖНО про расход лимита (см. client.py и план, фаза 3/4): один вызов
get_fixture(..., include=HEAVY_FIXTURE_INCLUDE) на КАЖДЫЙ матч сезона — это
осознанно, бэкафилл истории делается ОДИН РАЗ и не подчиняется правилу
двухуровневого live-опроса (то правило — только для фазы 4, регулярного
поллинга). Сезон КПЛ — around 200-240 матчей, 3 сезона — до ~700 тяжёлых
запросов суммарно, далеко от лимита 2000/час на entity Fixture (см.
SportmonksClient._log_rate_limit_if_low — если лимит всё же начнёт
подходить к концу, в лог уйдёт warning).
"""
import logging
from datetime import date, datetime, timedelta

from django.core.management.base import BaseCommand

from leagues.models import League
from parsers.sportmonks import importers
from parsers.sportmonks.client import SportmonksAPIError, SportmonksClient

logger = logging.getLogger(__name__)

# НАЙДЕНО ВЖИВУЮ (2026-09-08, первый реальный прогон): /fixtures/between
# отказывает с HTTP 422, если диапазон дат шире 100 дней ("You requested a
# date range of 238 days. The maximum range is 100 days") — это не было
# видно ни в документации, ни в предыдущих тестах (там диапазоны были
# короче). Полный сезон КПЛ (март-октябрь) — почти 240 дней, то есть
# ВСЕГДА шире лимита. Дробим на куски по 90 дней (с запасом от границы в
# 100) и склеиваем результат, а не гадаем маленький лимит впритык.
MAX_DATE_RANGE_DAYS = 90


def _chunk_date_range(date_from: str, date_to: str, max_days: int = MAX_DATE_RANGE_DAYS):
    """Дробит [date_from, date_to] ("YYYY-MM-DD") на последовательные куски
    не длиннее max_days дней каждый, включительно с обеих сторон."""
    start = datetime.strptime(date_from, "%Y-%m-%d").date()
    end = datetime.strptime(date_to, "%Y-%m-%d").date()
    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + timedelta(days=max_days - 1), end)
        yield cursor.isoformat(), chunk_end.isoformat()
        cursor = chunk_end + timedelta(days=1)


def _resolve_current_season_id(all_seasons_data: list):
    """НАЙДЕННЫЙ БАГ (2026-09-08, пользователь: "страницы сборная сезона и
    тура выдают ошибку потому что пустые"): этот командный файл раньше
    вызывал get_or_create_season БЕЗ is_current вообще — ни один сезон не
    получал is_active=True, Season.get_primary_active() всегда возвращал
    None, страницы season_squad/round_squad (которые резолвят сезон по
    умолчанию именно через неё) не находили сезон. Здесь определяем
    "текущий" сезон по датам (сегодня внутри [starting_at, ending_at]),
    независимо от --year фильтра пользователя (даже если гоняем только
    --year 2024, 2026 должен остаться активным, а 2024 — нет)."""
    today = date.today()
    fallback: tuple | None = None
    for s in all_seasons_data:
        start_raw, end_raw = s.get("starting_at"), s.get("ending_at")
        if not start_raw or not end_raw:
            continue
        start_d = datetime.strptime(start_raw[:10], "%Y-%m-%d").date()
        end_d = datetime.strptime(end_raw[:10], "%Y-%m-%d").date()
        if start_d <= today <= end_d:
            return s["id"]
        if start_d <= today and (fallback is None or start_d > fallback[0]):
            fallback = (start_d, s["id"])
    if fallback:
        return fallback[1]
    return all_seasons_data[-1]["id"] if all_seasons_data else None


class Command(BaseCommand):
    help = "Бэкафилл истории КПЛ из Sportmonks (фаза 3 миграции, см. docs/sportmonks-migration-plan.md)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--year", type=str, default=None,
            help="Только один сезон, например 2026. По умолчанию — все доступные (2024/2025/2026).",
        )
        parser.add_argument(
            "--limit", type=int, default=None,
            help="Ограничить число матчей на сезон (для теста на 5-6 матчах перед полным прогоном).",
        )

    def handle(self, *args, **options):
        client = SportmonksClient()
        year_filter = options.get("year")
        limit = options.get("limit")

        try:
            league_data = client.get_league(include="seasons")
        except SportmonksAPIError as exc:
            self.stderr.write(self.style.ERROR(f"Не удалось получить лигу: {exc}"))
            return

        league = importers.get_or_create_league(league_data)
        self.stdout.write(f"Лига: {league.name} (sportmonks_id={league.sportmonks_id})")

        all_seasons_data = league_data.get("seasons") or []
        current_season_id = _resolve_current_season_id(all_seasons_data)

        seasons_data = all_seasons_data
        if year_filter:
            seasons_data = [s for s in seasons_data if year_filter in (s.get("name") or "")]
        if not seasons_data:
            self.stderr.write(self.style.ERROR(
                "Не нашёл сезонов по фильтру — проверьте --year или доступность сезонов в /leagues/393?include=seasons"
            ))
            return

        total_imported = 0
        total_failed = 0

        for season_data in seasons_data:
            is_current = season_data["id"] == current_season_id
            season = importers.get_or_create_season(season_data, league, is_current=is_current)
            if is_current:
                self.stdout.write(self.style.SUCCESS(f"  (текущий активный сезон сайта: {season.year})"))
            self.stdout.write(f"\nСезон {season.year} (sportmonks id={season_data['id']}) — тяну календарь...")

            date_from = (season_data.get("starting_at") or "")[:10]
            date_to = (season_data.get("ending_at") or "")[:10]
            if not date_from or not date_to:
                self.stderr.write(self.style.WARNING(
                    f"  Сезон {season.year}: нет starting_at/ending_at в ответе API, пропущен"
                ))
                continue

            chunks = list(_chunk_date_range(date_from, date_to))
            fixtures_by_id = {}
            calendar_error = False
            for chunk_from, chunk_to in chunks:
                try:
                    chunk_fixtures = client.get_fixtures_between(chunk_from, chunk_to, include="participants")
                except SportmonksAPIError as exc:
                    calendar_error = True
                    self.stderr.write(self.style.ERROR(
                        f"  Сезон {season.year}: ошибка получения календаря {chunk_from}..{chunk_to} — {exc}"
                    ))
                    continue
                for fx in chunk_fixtures:
                    fixtures_by_id[fx["id"]] = fx

            if calendar_error and not fixtures_by_id:
                continue

            fixtures = list(fixtures_by_id.values())
            self.stdout.write(f"  Матчей в календаре: {len(fixtures)} (запросов по датам: {len(chunks)})")
            if limit:
                fixtures = fixtures[:limit]
                self.stdout.write(f"  Ограничение --limit={limit}: возьму первые {len(fixtures)}")

            season_imported = 0
            season_failed = 0
            for i, fx in enumerate(fixtures, start=1):
                fixture_id = fx.get("id")
                try:
                    full = client.get_fixture(fixture_id, include=importers.HEAVY_FIXTURE_INCLUDE)
                    importers.import_full_fixture(full, league=league, season=season)
                    season_imported += 1
                except SportmonksAPIError as exc:
                    season_failed += 1
                    logger.error("Sportmonks: fixture %s — ошибка API: %s", fixture_id, exc)
                    self.stderr.write(self.style.WARNING(f"    [{i}/{len(fixtures)}] fixture {fixture_id}: {exc}"))
                except Exception as exc:
                    season_failed += 1
                    logger.error("Sportmonks: fixture %s — ошибка импорта", fixture_id, exc_info=True)
                    self.stderr.write(self.style.ERROR(f"    [{i}/{len(fixtures)}] fixture {fixture_id}: {exc}"))

                if i % 20 == 0:
                    self.stdout.write(f"    ...{i}/{len(fixtures)}")

            self.stdout.write(self.style.SUCCESS(
                f"  Сезон {season.year}: импортировано {season_imported}/{len(fixtures)}"
                + (f", ошибок: {season_failed}" if season_failed else "")
            ))
            total_imported += season_imported
            total_failed += season_failed

        self.stdout.write(self.style.SUCCESS(
            f"\nГотово. Всего импортировано матчей: {total_imported}"
            + (f", ошибок: {total_failed}" if total_failed else "")
        ))
