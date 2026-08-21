# loadtest/locustfile.py
"""
Нагрузочное тестирование DOPX через Locust — имитация реальных
пользователей (браузинг + логин + голосование за события + полное
прохождение вайзарда оценки матча, включая "фрод"-сценарий для проверки
анти-фрод сигнала по скорости).

ПОДГОТОВКА (один раз перед первым запуском, и повторно если нужно больше
тестовых аккаунтов):
    python manage.py setup_load_test --users 200

ЗАПУСК:
    pip install locust
    locust -f loadtest/locustfile.py --host http://127.0.0.1:8000

Откройте http://localhost:8089 — там задаётся число пользователей и
скорость набора (spawn rate) ЖИВЬЁМ, можно менять на лету, не
перезапуская. Для автоматического ступенчатого роста (10 -> 100 -> 300 ->
1000) используйте headless-режим с классом StagedLoadShape ниже:

    locust -f loadtest/locustfile.py --host http://127.0.0.1:8000 --headless

ВАЖНО про окружение:
- `manage.py runserver` — МНОГОпоточный (не однопоточный, как тут было
  написано раньше — неточность), но каждый поток может открыть СВОЁ
  соединение к Postgres (CONN_MAX_AGE=600 в settings.py держит их живыми),
  и это соединение НИЧЕМ не ограничено сверху. При 100+ конкурентных ботах
  это легко упирается в max_connections Postgres ("too many clients
  already") — под нагрузочным тестом это ОЖИДАЕМЫЙ артефакт runserver, а
  не баг проекта: в проде вы будете стоять за gunicorn с ФИКСИРОВАННЫМ
  числом воркеров, там соединений ровно столько, сколько воркеров, и
  никакого исчерпания. Чтобы получить локально ЧИСТЫЕ, заслуживающие
  доверия цифры — гоняйте через gunicorn с ограниченным числом воркеров
  (`gunicorn dopx.wsgi -w 4 --threads 2`), а не через runserver. Финальный
  контрольный прогон — уже на арендованном VPS, один в один как в проде.
- Часть системы (django-axes, rate-limit, honeypot+капча на регистрации)
  СОЗНАТЕЛЬНО мешает грубому боту — это не баг теста. Капчу автоматически
  не проходим (см. ниже) — регистрация через UI тестируется вручную
  отдельно, не этим инструментом.
- Локально все боты идут с одного IP (127.0.0.1) — IP-based rate-limit
  (password-reset, verify-email) будет валиться быстрее, чем в проде с
  реальными разными IP. В сценарии ниже такие эндпоинты не гоняем массово
  по этой же причине — тест был бы не про ёмкость системы, а про то, что
  один IP тут же упирается в лимит (ожидаемо и уже проверено).

ЧТО СМОТРЕТЬ ВО ВРЕМЯ ТЕСТА:
- Locust Web UI: RPS, время ответа (p50/p95/p99), % failures — по каждому
  эндпоинту отдельно.
- Консоль `manage.py runserver`/gunicorn: warning-и от QueryCountMiddleware
  (SLOW/HIGH-QUERY REQUEST) и CacheHitMiddleware (LOW CACHE HIT RATE) —
  оба уже встроены в проект (dopx/middleware.py) специально для этого.
- Django admin -> Staff-дашборд -> Здоровье данных / Антифрод — там будет
  видно, как реагирует flag_suspicious_wizard_speed_task на "фрод"-ботов.
- Postgres/Redis: `top`/`htop`, число активных соединений к Postgres
  (`SELECT count(*) FROM pg_stat_activity;`) — не должно расти неограниченно.
"""
from __future__ import annotations

import random
import re

import gevent
from locust import HttpUser, LoadTestShape, task, between

# Должны совпадать с core/management/commands/setup_load_test.py
LOAD_TEST_MATCH_ID = "10000000-0000-0000-0000-000000000001"
LOAD_TEST_USER_COUNT = 200  # держите в синхроне с --users при запуске setup_load_test
LOAD_TEST_PASSWORD = "LoadTest2026!"

WIZARD_STEPS = ["context", "teams", "players", "coaches", "referee", "match_eval"]


def _csrf_headers(client) -> dict:
    """Django принимает CSRF-токен из cookie через заголовок X-CSRFToken —
    не нужно парсить hidden input из HTML на каждый шаг."""
    token = client.cookies.get("csrftoken")
    return {"X-CSRFToken": token} if token else {}


class DopxUser(HttpUser):
    """
    Обычный пользователь: логинится один раз тестовым аккаунтом, дальше
    браузит сайт и изредка голосует/реагирует. Вес задач подобран так, чтобы
    чтение сильно преобладало над записью — как в реальном трафике.
    """
    wait_time = between(1, 4)

    def on_start(self):
        username = f"loadtest_{random.randint(1, LOAD_TEST_USER_COUNT):04d}"
        # GET нужен, чтобы получить csrftoken-cookie перед POST /users/login/.
        self.client.get("/users/login/", name="/users/login/ [GET]")
        self.client.post(
            "/users/login/",
            data={"username": username, "password": LOAD_TEST_PASSWORD},
            headers=_csrf_headers(self.client),
            name="/users/login/ [POST]",
        )
        self.username = username

    # --- Чтение (основной вес) -------------------------------------------------

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
        # То же, что live-пульс на странице матча опрашивает в фоне.
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

    # --- Запись (реже) ----------------------------------------------------------

    @task(4)
    def react_to_random_event(self):
        # match_events_partial отдаёт HTML с data-event-id — вытаскиваем
        # регуляркой, чтобы не хардкодить ID событий (их создаёт парсер).
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
    """
    Проходит вайзард оценки как обычный человек — с реалистичной паузой
    между шагами (2-8с на шаг, читает, крутит слайдеры). Большая часть
    "голосующих" ботов должна быть такого типа.
    """
    weight = 5

    @task(1)
    def full_wizard_human_pace(self):
        _run_wizard(self.client, human_pace=True)


class FraudWizardUser(DopxUser):
    """
    Намеренно "жульничает" — проходит весь 6-шаговый вайзард почти без
    пауз (как реальный скрипт-накрутчик). Меньшинство ботов такого типа —
    именно для проверки, что flag_suspicious_wizard_speed_task ловит это
    ПОД НАГРУЗКОЙ (не только в единичном запросе, как в тестах evaluations),
    и что это не создаёт гонки/дедлоков на EvaluationSession под
    конкурентным доступом нескольких таких ботов одновременно.
    """
    weight = 1

    @task(1)
    def full_wizard_fraud_pace(self):
        _run_wizard(self.client, human_pace=False)


def _run_wizard(client, human_pace: bool) -> None:
    """
    human_pace=True — реалистичная пауза 2-6с между шагами (читает,
    двигает слайдеры). human_pace=False — пауза ~0.05-0.2с, имитация
    скрипта-накрутчика: именно эту разницу должен ловить
    flag_suspicious_wizard_speed_task на шаге 6.
    """
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

    # Шаг 2: команды (динамические поля по обеим командам матча — имена полей
    # заранее известны только для LOAD_TEST_MATCH_ID, т.к. читаем их из
    # setup_load_test.py: home/away team id зашиты туда же).
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

    # Шаг 3: игроки — достаём реальные player_id со страницы (генерятся
    # setup_load_test.py неслучайно, но проще прочитать со страницы, чем
    # дублировать логику присвоения ID).
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

    # Шаг 4: тренеры (coach id тоже стабильны только по имени — читаем со
    # страницы, чтобы не хардкодить).
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

    # Шаг 6: финал — именно тут ставится flag_suspicious_wizard_speed_task.
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
    """
    Ступенчатый рост нагрузки для headless-режима: 10 -> 50 -> 100 -> 300 ->
    1000 одновременных "пользователей", каждая ступень держится 3 минуты,
    прирост (spawn rate) — по 10 пользователей/сек. Чтобы не участвовала в
    обычном Web-UI режиме (там числа задаются руками) — Locust сам не
    подключает LoadTestShape, если запущен НЕ headless и вы явно не выбрали
    его в UI, так что этот класс безопасно оставлять в файле всегда.
    """
    # "duration" здесь — АБСОЛЮТНАЯ метка времени с начала прогона (не длина
    # самой ступени), поэтому значения по возрастанию: до 180с держим 10
    # пользователей, с 180 до 360с — 50, и т.д.
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
