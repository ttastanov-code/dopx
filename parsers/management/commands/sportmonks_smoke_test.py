# parsers/management/commands/sportmonks_smoke_test.py
"""manage.py sportmonks_smoke_test

Проверка Sportmonks: токен, лига, текущий сезон. Ничего не пишет.
"""
import logging

from django.core.management.base import BaseCommand

from parsers.sportmonks.client import SportmonksAPIError, SportmonksClient

logging.basicConfig(level=logging.INFO)


class Command(BaseCommand):
    help = "Проверка подключения к Sportmonks API (фаза 1 миграции, ничего не пишет в БД)"

    def handle(self, *args, **options):
        client = SportmonksClient()

        try:
            league = client.get_league(include='currentSeason')
        except SportmonksAPIError as exc:
            self.stderr.write(self.style.ERROR(f"Не удалось получить лигу: {exc}"))
            self.stderr.write(self.style.WARNING(
                "Проверьте SPORTMONKS_API_TOKEN в .env и что подписка/трайл ещё активны."
            ))
            return

        self.stdout.write(self.style.SUCCESS(f"Лига: {league.get('name')} (id={league.get('id')})"))

        season = league.get('currentseason') or {}
        if season:
            self.stdout.write(
                f"Текущий сезон: {season.get('name')} (id={season.get('id')}), "
                f"с {season.get('starting_at')} по {season.get('ending_at')}"
            )
        else:
            self.stderr.write(self.style.WARNING(
                "currentSeason не пришёл в ответе — план подписки может не давать доступа к этой лиге."
            ))
            return

        try:
            live = client.get_livescores()
        except SportmonksAPIError as exc:
            self.stderr.write(self.style.ERROR(f"livescores не отработал: {exc}"))
            return

        self.stdout.write(f"Сейчас live-матчей КПЛ: {len(live)}")
        self.stdout.write(self.style.SUCCESS("Sportmonks-клиент настроен и работает."))
