# parsers/sportmonks/client.py
"""Клиент Sportmonks Football API v3.

Лимит — 2000 запросов/час на каждую entity. Live-опрос — только через
get_livescores() (один вызов на все матчи), не циклом по fixture.
"""
import logging
import time
from typing import Any, Optional

import requests
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)

# Свой счётчик запросов в Redis (час/сутки) — API не отдаёт общий расход.
_REQUEST_COUNTER_HOUR_TIMEOUT = 60 * 65  # чуть больше часа
_REQUEST_COUNTER_DAY_TIMEOUT = 60 * 60 * 26  # чуть больше суток


def _request_counter_keys() -> tuple[str, str]:
    now = timezone.now()
    return (
        f"sportmonks:requests:hour:{now:%Y%m%d%H}",
        f"sportmonks:requests:day:{now:%Y%m%d}",
    )


def _track_request() -> None:
    """+1 к счётчикам текущего часа и суток (UTC)."""
    hour_key, day_key = _request_counter_keys()
    try:
        cache.incr(hour_key)
    except ValueError:
        cache.set(hour_key, 1, timeout=_REQUEST_COUNTER_HOUR_TIMEOUT)
    try:
        cache.incr(day_key)
    except ValueError:
        cache.set(day_key, 1, timeout=_REQUEST_COUNTER_DAY_TIMEOUT)


def get_request_counts() -> dict:
    """Сколько запросов мы сделали за текущий час/сутки (для дашборда)."""
    hour_key, day_key = _request_counter_keys()
    return {
        "hour": cache.get(hour_key, 0),
        "day": cache.get(day_key, 0),
    }


class SportmonksAPIError(Exception):
    """Ошибка запроса после всех ретраев. Вызывающий код решает, пропустить или прервать."""
    pass


# Ретраим 429 и 5xx. 4xx не ретраим — ответ не изменится.
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 1.5  # умножается на номер попытки


class SportmonksClient:
    """Клиент https://api.sportmonks.com/v3/football.

        client = SportmonksClient()
        data = client.get_fixture(19681993, include="participants;events.player")
    """

    def __init__(self, api_token: Optional[str] = None, base_url: Optional[str] = None):
        self.api_token = api_token or settings.SPORTMONKS_API_TOKEN
        self.base_url = (base_url or settings.SPORTMONKS_BASE_URL).rstrip('/')
        self.locale = getattr(settings, 'SPORTMONKS_LOCALE', 'ru')
        self.session = requests.Session()
        # Остаток лимита по последней entity (для дашборда).
        self.last_rate_limit: Optional[dict] = None

    # -- низкоуровневый запрос -------------------------------------------------

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        if not self.api_token:
            raise SportmonksAPIError("SPORTMONKS_API_TOKEN не задан в настройках/окружении")

        url = f"{self.base_url}{path}"
        request_params = {'api_token': self.api_token, 'locale': self.locale}
        if params:
            request_params.update(params)

        last_error: Optional[Exception] = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = self.session.get(url, params=request_params, timeout=15)
            except requests.RequestException as exc:
                last_error = exc
                logger.warning("Sportmonks: сетевая ошибка на %s (попытка %s/%s): %s", path, attempt, MAX_RETRIES, exc)
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
                continue

            # Считаем каждый запрос, получивший HTTP-ответ.
            _track_request()

            if response.status_code in RETRYABLE_STATUS_CODES:
                last_error = SportmonksAPIError(f"HTTP {response.status_code} на {path}")
                logger.warning(
                    "Sportmonks: %s на %s (попытка %s/%s), ретраим",
                    response.status_code, path, attempt, MAX_RETRIES,
                )
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
                continue

            if response.status_code >= 400:
                # 4xx — сразу ошибка с телом ответа.
                raise SportmonksAPIError(f"HTTP {response.status_code} на {path}: {response.text[:500]}")

            payload = response.json()
            self._log_rate_limit_if_low(path, payload)
            return payload

        raise SportmonksAPIError(f"Не удалось получить {path} после {MAX_RETRIES} попыток: {last_error}")

    def _get_data(self, path: str, params: Optional[dict] = None) -> dict:
        """_get() для одиночной сущности. Нет 'data' в ответе — SportmonksAPIError с телом."""
        payload = self._get(path, params)
        if 'data' not in payload:
            raise SportmonksAPIError(
                f"Ответ {path} не содержит поля 'data' — тело ответа: {str(payload)[:500]}"
            )
        return payload['data']

    def _get_all_pages(self, path: str, params: Optional[dict] = None) -> list:
        """Все страницы списочного эндпоинта (по 25 на страницу).
        max_pages — защита от бесконечного цикла.
        """
        all_data: list = []
        page = 1
        max_pages = 40
        base_params = dict(params or {})
        while page <= max_pages:
            page_params = dict(base_params)
            if page > 1:
                page_params['page'] = page
            payload = self._get(path, page_params)
            page_data = payload.get('data') or []
            if isinstance(page_data, dict):
                # Одиночная сущность — data это dict.
                return [page_data]
            all_data.extend(page_data)

            pagination = payload.get('pagination') or {}
            if not pagination.get('has_more'):
                break
            page += 1
        else:
            logger.warning(
                "Sportmonks: _get_all_pages(%s) остановлен на пределе %d страниц — "
                "возможно, пагинация не заканчивается как ожидалось",
                path, max_pages,
            )
        return all_data

    def _log_rate_limit_if_low(self, path: str, payload: dict) -> None:
        """Сохраняет rate_limit ответа в last_rate_limit и пишет warning при низком остатке."""
        rate_limit = payload.get('rate_limit') or {}
        if rate_limit:
            self.last_rate_limit = rate_limit
        remaining = rate_limit.get('remaining')
        entity = rate_limit.get('requested_entity')
        if remaining is not None and remaining < 200:
            logger.warning(
                "Sportmonks: остаток лимита по entity=%s всего %s (запрос %s) — "
                "проверить, не пошёл ли где-то опрос в цикле по матчам вместо bulk-вызова",
                entity, remaining, path,
            )

    # -- лиги/сезоны -------------------------------------------------------

    def get_league(self, league_id: Optional[int] = None, include: Optional[str] = None) -> dict:
        league_id = league_id or settings.SPORTMONKS_LEAGUE_ID
        params = {'include': include} if include else None
        return self._get_data(f"/leagues/{league_id}", params)

    def get_seasons(self, league_id: Optional[int] = None) -> list:
        return self.get_league(league_id, include='seasons').get('seasons', [])

    def get_standings(self, season_id: int, include: str = 'form;details.type') -> list:
        return self._get_data(f"/standings/seasons/{season_id}", {'include': include})

    def get_season_teams(self, season_id: int) -> list:
        """Команды лиги в сезоне (одним вызовом)."""
        rows = self.get_standings(season_id, include='participant')
        return [
            {'id': row['participant_id'], 'name': (row.get('participant') or {}).get('name')}
            for row in rows
            if row.get('participant')
        ]

    # -- матчи ---------------------------------------------------------------

    def get_livescores(self, include: str = 'events;participants;scores', league_id: Optional[int] = None) -> list:
        """Все live-матчи лиги одним вызовом. Пустой список — нет матчей.
        Фильтр по лиге — первый барьер от чужих матчей, дальше проверка в tasks и importers.
        """
        league_id = league_id or settings.SPORTMONKS_LEAGUE_ID
        params = {'include': include, 'filters': f'fixtureLeagues:{league_id}'}
        return self._get_all_pages('/livescores/inplay', params)

    def get_fixtures_between(
        self, date_from: str, date_to: str, league_id: Optional[int] = None,
        include: Optional[str] = None,
    ) -> list:
        """Матчи в диапазоне дат (YYYY-MM-DD), все страницы."""
        league_id = league_id or settings.SPORTMONKS_LEAGUE_ID
        params = {'filters': f'fixtureLeagues:{league_id}'}
        if include:
            params['include'] = include
        return self._get_all_pages(f"/fixtures/between/{date_from}/{date_to}", params)

    def get_fixture(self, fixture_id: int, include: Optional[str] = None) -> dict:
        """Полные данные по одному матчу. Вызывать только при изменении состояния."""
        params = {'include': include} if include else None
        return self._get_data(f"/fixtures/{fixture_id}", params)

    # -- команды/составы/недоступность игроков --------------------------------

    def get_team(self, team_id: int, include: Optional[str] = None) -> dict:
        params = {'include': include} if include else None
        return self._get_data(f"/teams/{team_id}", params)

    def get_squad(self, season_id: int, team_id: int, include: str = 'player') -> list:
        # Ростер может быть больше одной страницы.
        return self._get_all_pages(f"/squads/seasons/{season_id}/teams/{team_id}", {'include': include})

    def get_sidelined(self, team_id: int) -> list:
        """Травмы и дисквалификации игроков команды."""
        return self.get_team(team_id, include='sidelined.player').get('sidelined', [])

    # -- судьи/тренеры (одиночная сущность) ------------------------------------
    # Для точечного перезапроса имени по sportmonks_id.
    def get_referee(self, referee_id: int) -> dict:
        return self._get_data(f"/referees/{referee_id}")

    def get_coach(self, coach_id: int) -> dict:
        return self._get_data(f"/coaches/{coach_id}")

    def get_player(self, player_id: int) -> dict:
        return self._get_data(f"/players/{player_id}")
