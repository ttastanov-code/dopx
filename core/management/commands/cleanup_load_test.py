# core/management/commands/cleanup_load_test.py
"""
Удаляет ВСЕ данные, созданные setup_load_test.py и последующим прогоном
Locust: тестовых пользователей (username startswith 'loadtest_') — вместе с
ними каскадом уходят их оценки, XP, уведомления, подписки, сессии вайзарда,
флаги антифрода, аналитика (всё, что висит на FK к User). Отдельно удаляет
синтетический load-test матч и всё привязанное к нему (состав, команды,
тренеров, судью, лигу, сезон) — строго по фиксированным UUID из
setup_load_test.py, поэтому реальные данные парсера гарантированно не
затрагиваются (случайные UUID реальных объектов с этими не пересекаются).

Запуск: python manage.py cleanup_load_test
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.db import transaction
from django.core.management.base import BaseCommand

from coaches.models import Coach
from leagues.models import League
from lineups.models import MatchLineup, MatchLineupPlayer
from matches.models import Match
from players.models import Player
from referees.models import Referee
from seasons.models import Season
from teams.models import Team

from core.management.commands.setup_load_test import (
    LOAD_TEST_MATCH_ID,
    LOAD_TEST_LEAGUE_ID,
    LOAD_TEST_HOME_TEAM_ID,
    LOAD_TEST_AWAY_TEAM_ID,
    LOAD_TEST_USERNAME_PREFIX,
)

User = get_user_model()


class Command(BaseCommand):
    help = "Удаляет тестовых пользователей и синтетический матч, созданные setup_load_test.py / Locust-прогоном."

    def handle(self, *args, **options):
        with transaction.atomic():
            users_qs = User.objects.filter(username__startswith=LOAD_TEST_USERNAME_PREFIX)
            user_count = users_qs.count()
            _, details = users_qs.delete()
            self.stdout.write(f"Пользователей удалено: {user_count} (объектов каскадом: {sum(details.values())})")
            for model_label, count in sorted(details.items()):
                if count:
                    self.stdout.write(f"  {model_label}: {count}")

            # Явно, детьми вперёд — на случай, если где-то PROTECT вместо CASCADE.
            MatchLineupPlayer.objects.filter(lineup__match_id=LOAD_TEST_MATCH_ID).delete()
            MatchLineup.objects.filter(match_id=LOAD_TEST_MATCH_ID).delete()
            match_deleted, _ = Match.objects.filter(id=LOAD_TEST_MATCH_ID).delete()
            self.stdout.write(f"Load-test матч удалён: {match_deleted}")

            Player.objects.filter(team_id__in=[LOAD_TEST_HOME_TEAM_ID, LOAD_TEST_AWAY_TEAM_ID]).delete()
            Coach.objects.filter(team_id__in=[LOAD_TEST_HOME_TEAM_ID, LOAD_TEST_AWAY_TEAM_ID]).delete()
            # Судья не привязан к команде load-test'а — узнаём по имени,
            # заданному в setup_load_test.py (единственное место, где оно
            # создаётся, коллизия с реальным судьёй практически исключена).
            Referee.objects.filter(first_name="Load", last_name="Referee").delete()
            Team.objects.filter(id__in=[LOAD_TEST_HOME_TEAM_ID, LOAD_TEST_AWAY_TEAM_ID]).delete()
            Season.objects.filter(league_id=LOAD_TEST_LEAGUE_ID).delete()
            League.objects.filter(id=LOAD_TEST_LEAGUE_ID).delete()

        self.stdout.write(self.style.SUCCESS("Готово: все тестовые данные удалены, реальные не тронуты."))
