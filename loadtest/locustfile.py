# loadtest/locustfile.py
"""Нагрузочный тест Locust: браузинг, логин, реакции, вайзард оценки (в т.ч. «фрод»-боты).

Подготовка:
    python manage.py setup_load_test --users 200
Запуск:
    pip install locust
    locust -f loadtest/locustfile.py --host http://127.0.0.1:8000
UI — http://localhost:8089. Ступенчатый рост — --headless (StagedLoadShape).

Для честных цифр — gunicorn с фиксированным числом воркеров, не runserver
(runserver упирается в max_connections Postgres).
Локально все боты с одного IP — IP rate-limit срабатывает быстрее.
Смотреть: RPS/p95 в Locust, warning-и QueryCountMiddleware/CacheHitMiddleware,
антифрод в дашборде, число соединений в pg_stat_activity.
"""
from __future__ import annotations

import random
import re

import gevent
from locust import HttpUser, LoadTestShape, task, between

# Совпадают с core/management/commands/setup_load_test.py
LOAD_TEST_MATCH_ID = "10000000-0000-0000-0000-000000000001"
LOAD_TEST_USER_COUNT = 200  # в синхроне с --users у setup_load_test
LOAD_TEST_PASSWORD = "LoadTest2026!"

WIZARD_STEPS = ["context", "teams", "players", "coaches", "referee", "match_eval"]


def _csrf_headers(client) -> dict:
    """CSRF-токен из cookie в заголовке X-CSRFToken."""
    token = client.cookies.get("csrftoken")
    return {"X-CSRFToken": token} if token else {}


class DopxUser(HttpUser):
    """Обычный пользователь: логин, в основном чтение, изредка запись."""
    wait_time = between(1, 4)

    def on_start(self):
        username = f"loadtest_{random.randint(1, LOAD_TEST_USER_COUNT):04d}"
        # GET — чтобы получить csrftoken перед логином.
        self.client.get("/users/login/", name="/users/login/ [GET]")
        self.client.post(
            "/users/login/",
            data={"username": username, "password": LOAD_TEST_PASSWORD},
            headers=_csrf_headers(self.client),
            name="/users/login/ [POST]",
        )
        self.username = username

    # --- Чтение -----------------------------------------------------------------

    @task(10)
    def browse_home(self):
        self.client.get("/", name="/ (home)")

    @task(6)
    def browse_matches_list(self):
        self.client.get("/matches/", name="/matches/")

    @task(6)
    def view_load_test_match(self):
        self.client.get(f"/matches/{LOAD_TEST_MATCH_ID}/", name="/matches/<id>/")

    @task(3)
    def view_match_events_partial(self):
        # Фоновый live-пульс страницы матча.
        self.client.get(f"/matches/{LOAD_TEST_MATCH_ID}/events/", name="/matches/<id>/events/ [live-poll]")

    @task(4)
    def view_leaderboard(self):
        self.client.get("/users/leaderboard/", name="/users/leaderboard/")

    @task(2)
    def view_player_leaderboard(self):
        self.client.get("/users/players/leaderboard/", name="/users/players/leaderboard/")

    @task(3)
    def view_own_profile(self):
        self.client.get("/users/profile/", name="/users/profile/")

    @task(2)
    def view_notifications(self):
        self.client.get("/notifications/", name="/notifications/")

    # --- Запись ----------------------------------------------------------------

    @task(4)
    def react_to_random_event(self):
        # ID событий берём из HTML (data-event-id).
        resp = self.client.get(f"/matches/{LOAD_TEST_MATCH_ID}/events/", name="/matches/<id>/events/ [для react]")
        event_ids = re.findall(r'data-event-id="([0-9a-f-]{36})"', resp.text)
        if not event_ids:
            return
        event_id = random.choice(event_ids)
        reaction = random.choice(["like", "dislike"])
        self.client.post(
            f"/events/{event_id}/react/",
            data={"reaction": reaction},
            headers=_csrf_headers(self.client),
            name="/events/<id>/react/",
        )


class HumanWizardUser(DopxUser):
    """Проходит вайзард с человеческими паузами."""
    weight = 5

    @task(1)
    def full_wizard_human_pace(self):
        _run_wizard(self.client, human_pace=True)


class FraudWizardUser(DopxUser):
    """Проходит вайзард почти без пауз — проверка антифрода под нагрузкой."""
    weight = 1

    @task(1)
    def full_wizard_fraud_pace(self):
        _run_wizard(self.client, human_pace=False)


def _run_wizard(client, human_pace: bool) -> None:
    """human_pace=True — пауза 2-6 с, False — ~0.05-0.2 с (как скрипт)."""
    def _between_steps():
        gevent.sleep(random.uniform(2.0, 6.0) if human_pace else random.uniform(0.05, 0.2))

    match_id = LOAD_TEST_MATCH_ID

    # Шаг 1: контекст
    client.get(f"/evaluations/match/{match_id}/context/", name="/evaluations/.../context/ [GET]")
    client.post(
        f"/evaluations/match/{match_id}/context/",
        data={"watched_type": "tv", "attended_stadium": False},
        headers=_csrf_headers(client),
        name="/evaluations/.../context/ [POST]",
    )
    _between_steps()

    # Шаг 2: команды (id команд — из setup_load_test.py).
    from_home = "10000000-0000-0000-0000-000000000003"
    from_away = "10000000-0000-0000-0000-000000000004"
    team_payload = {}
    for team_id in (from_home, from_away):
        for field in ("tactics", "effort", "organization", "mentality"):
            team_payload[f"team_{team_id}_{field}"] = random.randint(1, 10)
    client.get(f"/evaluations/match/{match_id}/teams/", name="/evaluations/.../teams/ [GET]")
    client.post(
        f"/evaluations/match/{match_id}/teams/", data=team_payload,
        headers=_csrf_headers(client), name="/evaluations/.../teams/ [POST]",
    )
    _between_steps()

    # Шаг 3: игроки — id читаем со страницы.
    resp = client.get(f"/evaluations/match/{match_id}/players/", name="/evaluations/.../players/ [GET]")
    player_ids = re.findall(r'data-player-id="([0-9a-f-]{36})"', resp.text)
    players_payload = {}
    for pid in player_ids:
        players_payload[f"player_{pid}_evaluate"] = "on"
        players_payload[f"player_{pid}_contribution"] = random.randint(1, 10)
        players_payload[f"player_{pid}_risk"] = random.randint(1, 10)
        players_payload[f"player_{pid}_potential"] = random.randint(1, 10)
    client.post(
        f"/evaluations/match/{match_id}/players/", data=players_payload,
        headers=_csrf_headers(client), name="/evaluations/.../players/ [POST]",
    )
    _between_steps()

    # Шаг 4: тренеры — id читаем со страницы.
    resp = client.get(f"/evaluations/match/{match_id}/coaches/", name="/evaluations/.../coaches/ [GET]")
    coach_ids = set(re.findall(r'coach_([0-9a-f-]{36})_tactics', resp.text))
    coaches_payload = {}
    for cid in coach_ids:
        for field in ("tactics", "substitutions", "management", "impact"):
            coaches_payload[f"coach_{cid}_{field}"] = random.randint(1, 10)
    client.post(
        f"/evaluations/match/{match_id}/coaches/", data=coaches_payload,
        headers=_csrf_headers(client), name="/evaluations/.../coaches/ [POST]",
    )
    _between_steps()

    # Шаг 5: судья
    client.get(f"/evaluations/match/{match_id}/referee/", name="/evaluations/.../referee/ [GET]")
    client.post(
        f"/evaluations/match/{match_id}/referee/",
        data={"influence_score": random.randint(0, 100), "decision_quality": random.randint(1, 10)},
        headers=_csrf_headers(client), name="/evaluations/.../referee/ [POST]",
    )
    _between_steps()

    # Шаг 6: финал — здесь срабатывает проверка скорости.
    client.get(f"/evaluations/match/{match_id}/match/", name="/evaluations/.../match/ [GET]")
    client.post(
        f"/evaluations/match/{match_id}/match/",
        data={
            "entertainment": random.randint(1, 10), "tension": random.randint(1, 10),
            "turning_point": random.choice([True, False]), "fairness": random.randint(1, 10),
        },
        headers=_csrf_headers(client), name="/evaluations/.../match/ [POST]",
    )
    client.get(f"/evaluations/complete/{match_id}/", name="/evaluations/complete/<id>/")


class StagedLoadShape(LoadTestShape):
    """Ступенчатая нагрузка для headless: 10 -> 50 -> 100 -> 300 -> 1000, по 3 минуты."""
    # duration — время от начала прогона, а не длина ступени.
    stages = [
        {"duration": 180, "users": 10, "spawn_rate": 5},
        {"duration": 360, "users": 50, "spawn_rate": 10},
        {"duration": 540, "users": 100, "spawn_rate": 10},
        {"duration": 720, "users": 300, "spawn_rate": 10},
        {"duration": 900, "users": 1000, "spawn_rate": 10},
    ]

    def tick(self):
        run_time = self.get_run_time()
        for stage in self.stages:
            if run_time < stage["duration"]:
                return stage["users"], stage["spawn_rate"]
        return None
