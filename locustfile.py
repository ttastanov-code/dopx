# locustfile.py — нагрузка на API
from locust import HttpUser, task, between, events
import random
import logging

logger = logging.getLogger(__name__)

class DOPXUser(HttpUser):
    # Пауза между запросами
    wait_time = between(1, 3)
    
    # Таймауты
    request_timeout = 30
    
    @task(3)
    def get_player_aggregates(self):
        """Агрегаты игроков."""
        with self.client.get(
            "/api/player-aggregate/",
            catch_response=True,
            name="player_aggregates_list"
        ) as response:
            if response.status_code == 429:
                response.success()  # 429 не считаем ошибкой
                logger.warning("Rate limited (expected)")
            elif response.status_code != 200:
                response.failure(f"Got status {response.status_code}")
    
    @task(3)
    def get_match_aggregates(self):
        """Агрегаты матчей."""
        with self.client.get(
            "/api/match-aggregate/",
            catch_response=True,
            name="match_aggregates_list"
        ) as response:
            if response.status_code == 429:
                response.success()
            elif response.status_code != 200:
                response.failure(f"Got status {response.status_code}")
    
    @task(2)
    def get_top_players(self):
        """Топ игроков."""
        limit = random.choice([5, 10, 20])
        with self.client.get(
            f"/api/player-aggregate/top_players/?limit={limit}",
            catch_response=True,
            name="top_players"
        ) as response:
            if response.status_code == 429:
                response.success()
            elif response.status_code != 200:
                response.failure(f"Got status {response.status_code}")
    
    @task(2)
    def get_recent_matches(self):
        """Последние матчи."""
        limit = random.choice([5, 10])
        with self.client.get(
            f"/api/match-aggregate/recent/?limit={limit}",
            catch_response=True,
            name="recent_matches"
        ) as response:
            if response.status_code == 429:
                response.success()
            elif response.status_code != 200:
                response.failure(f"Got status {response.status_code}")
    
    @task(1)
    def get_player_analytics(self):
        """Аналитика игрока."""
        # Фиктивный player_id
        player_id = "00000000-0000-0000-0000-000000000001"
        with self.client.get(
            f"/api/player/analytics/?player_id={player_id}",
            catch_response=True,
            name="player_analytics"
        ) as response:
            # 404 — нормально
            if response.status_code in [200, 404, 429]:
                response.success()
            else:
                response.failure(f"Got status {response.status_code}")


# === Запуск ===

# Быстрый тест:
# locust -f locustfile.py --headless -u 20 -r 2 --run-time 60s --host http://127.0.0.1:8000

# Полный тест:
# locust -f locustfile.py --headless -u 50 -r 5 --run-time 180s --host http://127.0.0.1:8000
