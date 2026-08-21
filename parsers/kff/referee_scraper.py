# parsers/kff/referee_scraper.py
"""
СТАТУС (проверено 2026-08-21): судьи сейчас реально получаются через
JSON API — parsers/kff/importers.py::get_or_create_referee_by_name читает
game_data["referee"] из ответа /api/v1/games/{id} (см. client.py::
get_game_details). Ни один вызывающий код в проекте НЕ импортирует
scrape_referees — grep по всему репозиторию не нашёл ни одного вызова
(project_dump.txt не в счёт, это статичный текстовый дамп, а не код).
URL здесь тоже устарел: kffleague.kz/match/{id} не существует, реальный
роут — kffleague.kz/ru/matches/{id} (см. KFFClient.BASE_URL).

Рекомендация: удалить этот файл — HTML-скрейпинг избыточен, пока API
отдаёт то же самое надёжнее и без риска сломаться при вёрстке фронтенда
KFF. Не удалил автоматически (нет доступа для удаления файлов в
подключённой папке из этой сессии) — решение оставлено пользователю. Ниже
на всякий случай захардено (timeout/raise_for_status/retry) на случай,
если где-то вне этого репозитория (cron/отдельный скрипт) всё же
вызывается — но это не тот код, который реально работает в проде сегодня.
"""
import logging
import time

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

SCRAPE_TIMEOUT_SECONDS = 10
SCRAPE_MAX_RETRIES = 3


def scrape_referees(match_id):
    url = f"https://kffleague.kz/match/{match_id}"

    response = None
    for attempt in range(SCRAPE_MAX_RETRIES):
        try:
            response = requests.get(url, timeout=SCRAPE_TIMEOUT_SECONDS)
            response.raise_for_status()
            break
        except requests.exceptions.Timeout:
            logger.warning(f"⏱️ referee_scraper timeout (attempt {attempt + 1}/{SCRAPE_MAX_RETRIES}): {url}")
        except requests.exceptions.RequestException as e:
            logger.warning(
                f"⚠️ referee_scraper request failed (attempt {attempt + 1}/{SCRAPE_MAX_RETRIES}): "
                f"{type(e).__name__}: {e}"
            )
        if attempt < SCRAPE_MAX_RETRIES - 1:
            time.sleep(1 * (attempt + 1))

    if response is None:
        logger.error(f"❌ referee_scraper: все {SCRAPE_MAX_RETRIES} попытки провалились для {url}")
        return []

    soup = BeautifulSoup(response.text, "lxml")

    referees = []
    rows = soup.select(".match-referees li")

    for row in rows:
        name = row.text.strip()
        if not name:
            continue
        parts = name.split(" ")
        referees.append({
            "first_name": parts[0],
            "last_name": " ".join(parts[1:]),
            "role": "main",
        })

    return referees
