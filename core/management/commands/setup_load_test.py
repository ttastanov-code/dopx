# core/management/commands/setup_load_test.py
"""
Готовит данные для нагрузочного тестирования (см. loadtest/locustfile.py):

1. N тестовых пользователей (loadtest_0001..loadtest_000N) с известным
   паролем, email verified=True, с UserXP — заходят в систему БЕЗ капчи и
   без email-верификации, потому что это НЕ тест самой формы регистрации
   (капчу ботом не пройти по дизайну — это отдельный, ручной тест защиты),
   а тест системы под нагрузкой ЛОГИНОМ и обычными действиями.
2. Один синтетический "load-test матч" с ФИКСИРОВАННЫМ UUID — полностью
   завершённый, с открытым голосованием, полным составом (11+11 игроков),
   тренерами и судьёй — чтобы вайзард оценки был доступен ботам стабильно,
   независимо от реальных данных парсера и их таймингов.

Идемпотентно: можно запускать повторно, ничего не дублирует
(get_or_create везде). Порядок: League -> Season -> Team x2 -> Coach x2 ->
Referee -> Player x22 -> Match -> MatchLineup x2 -> MatchLineupPlayer x22.

Запуск: python manage.py setup_load_test [--users 100]
"""
from __future__ import annotations

import uuid
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from coaches.models import Coach
from leagues.models import League
from lineups.models import MatchLineup, MatchLineupPlayer
from matches.models import Match
from players.models import Player
from referees.models import Referee
from seasons.models import Season
from teams.models import Team
from users.models import UserXP

User = get_user_model()

# Фиксированный UUID — один и тот же load-test матч при каждом запуске
# команды и в locustfile.py. Не пересекается с реальными данными (парсер
# генерирует свои UUID случайно), поэтому коллизия исключена.
LOAD_TEST_MATCH_ID = uuid.UUID("10000000-0000-0000-0000-000000000001")
LOAD_TEST_LEAGUE_ID = uuid.UUID("10000000-0000-0000-0000-000000000002")
LOAD_TEST_HOME_TEAM_ID = uuid.UUID("10000000-0000-0000-0000-000000000003")
LOAD_TEST_AWAY_TEAM_ID = uuid.UUID("10000000-0000-0000-0000-000000000004")

LOAD_TEST_USERNAME_PREFIX = "loadtest_"
LOAD_TEST_PASSWORD = "LoadTest2026!"  # только для локальных/тестовых прогонов


class Command(BaseCommand):
    help = "Готовит тестовых пользователей и синтетический матч для нагрузочного тестирования (Locust)."

    def add_arguments(self, parser):
        parser.add_argument("--users", type=int, default=200, help="Сколько тестовых аккаунтов создать (по умолчанию 200).")

    def handle(self, *args, **options):
        n_users = options["users"]

        with transaction.atomic():
            self._create_users(n_users)
            self._create_load_test_match()

        self.stdout.write(self.style.SUCCESS(
            f"Готово: {n_users} тестовых пользователей (loadtest_0001..loadtest_{n_users:04d}, "
            f"пароль {LOAD_TEST_PASSWORD}), load-test матч {LOAD_TEST_MATCH_ID}."
        ))

    def _create_users(self, n_users: int) -> None:
        created = 0
        for i in range(1, n_users + 1):
            username = f"{LOAD_TEST_USERNAME_PREFIX}{i:04d}"
            user, was_created = User.objects.get_or_create(
                username=username,
                defaults={
                    "email": f"{username}@loadtest.dopx.local",
                    "is_verified": True,
                },
            )
            if was_created:
                user.set_password(LOAD_TEST_PASSWORD)
                user.save(update_fields=["password"])
                created += 1
            UserXP.objects.get_or_create(user=user)
        self.stdout.write(f"  Пользователи: {created} новых, всего {n_users}.")

    def _create_load_test_match(self) -> None:
        league, _ = League.objects.get_or_create(
            id=LOAD_TEST_LEAGUE_ID, defaults={"name": "Load Test League", "country": "Test"}
        )
        season, _ = Season.objects.get_or_create(league=league, year="2026")

        home_team, _ = Team.objects.get_or_create(id=LOAD_TEST_HOME_TEAM_ID, defaults={"name": "Load Test FC Home"})
        away_team, _ = Team.objects.get_or_create(id=LOAD_TEST_AWAY_TEAM_ID, defaults={"name": "Load Test FC Away"})

        home_coach, _ = Coach.objects.get_or_create(
            first_name="Home", last_name="Coach", defaults={"team": home_team}
        )
        away_coach, _ = Coach.objects.get_or_create(
            first_name="Away", last_name="Coach", defaults={"team": away_team}
        )
        referee, _ = Referee.objects.get_or_create(first_name="Load", last_name="Referee")

        now = timezone.now()
        match, _ = Match.objects.update_or_create(
            id=LOAD_TEST_MATCH_ID,
            defaults={
                "league": league,
                "season": season,
                "home_team": home_team,
                "away_team": away_team,
                "home_coach": home_coach,
                "away_coach": away_coach,
                "referee": referee,
                "start_time": now - timedelta(hours=2),
                "status": "finished",
                "home_score": 2,
                "away_score": 1,
                # Далеко в будущее — голосование НИКОГДА не закрывается само
                # по себе для этого матча, чтобы повторные прогоны нагрузки
                # не упирались в check_voting_access().
                "voting_open_until": now + timedelta(days=3650),
                "has_lineup": True,
            },
        )

        for team, side in [(home_team, "home"), (away_team, "away")]:
            lineup, _ = MatchLineup.objects.get_or_create(match=match, team=team, defaults={"side": side})
            existing_players = set(
                MatchLineupPlayer.objects.filter(lineup=lineup).values_list("player__first_name", flat=True)
            )
            for shirt_number in range(1, 12):
                player_name = f"P{shirt_number}"
                if player_name in existing_players:
                    continue
                player, _ = Player.objects.get_or_create(
                    first_name=player_name, last_name=side, team=team,
                    defaults={"position": "MF"},
                )
                MatchLineupPlayer.objects.get_or_create(
                    lineup=lineup, player=player,
                    defaults={"is_starting": True, "shirt_number": shirt_number},
                )

        self.stdout.write(f"  Load-test матч: {match.id} ({home_team.name} vs {away_team.name}), состав готов.")
