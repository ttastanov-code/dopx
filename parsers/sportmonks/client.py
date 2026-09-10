# parsers/sportmonks/client.py
"""
Клиент платного Sportmonks Football API (v3) — источник данных КПЛ,
которым по плану docs/sportmonks-migration-plan.md заменяется бесплатный
скрапинг KFF (parsers/kff/). НЕ путать по структуре с parsers/kff/client.py:
там половина модуля — защита от блокировки Cloudflare Bot Management на
бесплатном источнике (circuit breaker, TLS-имперсонация curl_cffi, cookie
jar, ротация прокси, джиттер между запросами). Против платного, официально
разрешённого API ничего из этого не нужно — Sportmonks сам ограничивает
только явным rate-limit (2000 запросов в час НА КАЖДУЮ entity отдельно,
подтверждено вживую тестовым ключом на реальных данных КПЛ), а не банит по
поведенческим сигналам. Поэтому этот клиент — обычный requests.Session с
понятным ретраем на транзиентные ошибки, и всё.

ВАЖНО про экономию лимита (см. план, фаза 4): у этого клиента намеренно
есть отдельный метод get_livescores() — ОДИН вызов возвращает ВСЕ идущие
сейчас матчи лиги разом, а не по одному на матч. Live-опрос обязан ходить
именно через него, а не в цикле по fixture id, иначе экономия исчезает
(проверено расчётом в переписке с пользователем: цикл по 6 матчам каждые
15 секунд — уже 1440 запросов/час, впритык к лимиту; тот же bulk-вызов —
240 запросов/час независимо от числа одновременных матчей).
"""
import logging
import time
from typing import Any, Optional

import requests
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)

# ИСПРАВЛЕНО (2026-09-09, баг найден пользователем — "вообще не сходится
# кол-во запросов израсходованных", скриншот собственной статистики
# Sportmonks: 871 запрос за сегодня против нашего "1 из 2000/час"): то, что
# показывалось раньше (last_rate_limit.remaining) — это остаток лимита
# ТОЛЬКО той entity, которую задел САМЫЙ ПОСЛЕДНИЙ вызов конкретного
# экземпляра SportmonksClient, а не суммарный расход по всем entity/задачам.
# Проверка доступности создаёт новый клиент и делает ровно один вызов
# get_league() — поэтому там физически не может быть числа больше 1-2,
# сколько бы live-опрос/синк ни расходовали в фоне через СВОИ экземпляры
# клиента. Официального агрегирующего эндпоинта расхода за день в Sportmonks
# v3 нет (сам портал считает это на своей стороне, не через API) — здесь
# считаем сами: инкрементируем общий счётчик в Redis на КАЖДЫЙ фактический
# HTTP-запрос, независимо от того, какой SportmonksClient и какая задача его
# сделали. Не будет побитово совпадать с порталом Sportmonks (у них могут
# быть другие границы суток/часа), но по порядку величины будет отражать
# реальный расход, а не "1".
_REQUEST_COUNTER_HOUR_TIMEOUT = 60 * 65  # чуть больше часа — переживает джиттер на границе
_REQUEST_COUNTER_DAY_TIMEOUT = 60 * 60 * 26  # чуть больше суток


def _request_counter_keys() -> tuple[str, str]:
    now = timezone.now()
    return (
        f"sportmonks:requests:hour:{now:%Y%m%d%H}",
        f"sportmonks:requests:day:{now:%Y%m%d}",
    )


def _track_request() -> None:
    """Инкремент двух счётчиков (текущий час / текущие сутки, по UTC-времени
    сервера) в общем Redis-кэше — переживает рестарт воркера/дев-сервера и
    виден из ЛЮБОГО экземпляра клиента, в отличие от last_rate_limit
    (атрибут инстанса). cache.incr бросает ValueError, если ключа ещё нет —
    это ожидаемо на первом запросе в новом часе/дне, просто заводим ключ."""
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
    """Для dashboard/parser_tools.py — сколько реальных HTTP-запросов к
    Sportmonks мы сами сделали за текущий час/сутки, посчитано на нашей
    стороне (см. _track_request докстринг)."""
    hour_key, day_key = _request_counter_keys()
    return {
        "hour": cache.get(hour_key, 0),
        "day": cache.get(day_key, 0),
    }


class SportmonksAPIError(Exception):
    """Итоговая (после исчерпания ретраев, либо не-транзиентная) ошибка
    запроса к Sportmonks. В отличие от KFFAPICircuitBreakerOpen у KFF-клиента,
    это не сигнал "хост нас забанил, останавливай весь прогон" — просто
    обычная ошибка одного запроса, вызывающий код (importers/tasks) решает
    сам, пропустить ли этот матч/сущность или прервать прогон целиком."""
    pass


# Коды ответа, на которые имеет смысл ретраить: 429 (rate limit, см.
# заголовок Retry-After обычно не нужен нам — окно короткое и наш расход
# далеко от лимита при правильном использовании get_livescores) и 5xx
# (временная проблема на стороне Sportmonks). НЕ ретраим 4xx — это осмысленные
# ответы API (неверный include, нет доступа по текущему плану, сущность не
# найдена), повторный запрос даст тот же результат.
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 1.5  # умножается на номер попытки


class SportmonksClient:
    """Тонкий клиент над https://api.sportmonks.com/v3/football.

    Использование:
        client = SportmonksClient()
        data = client.get_fixture(19681993, include="participants;events.player")
    """

    def __init__(self, api_token: Optional[str] = None, base_url: Optional[str] = None):
        self.api_token = api_token or settings.SPORTMONKS_API_TOKEN
        self.base_url = (base_url or settings.SPORTMONKS_BASE_URL).rstrip('/')
        self.locale = getattr(settings, 'SPORTMONKS_LOCALE', 'ru')
        self.session = requests.Session()
        # Заполняется после КАЖДОГО успешного _get() (см. ниже) — остаток
        # лимита по сущности, которую только что запросили. Не для алертинга
        # (для этого есть _log_rate_limit_if_low ниже) — просто чтобы
        # staff-дашборд мог показать "осталось X из 2000" без отдельного
        # запроса специально за этой информацией (см. dashboard/
        # parser_tools.py::sportmonks_api_health_check).
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

            # Считаем КАЖДЫЙ запрос, реально дошедший до сервера Sportmonks
            # (получен HTTP-ответ, любой статус) — именно так, судя по всему,
            # считает и портал Sportmonks (см. _track_request докстринг
            # выше): ретрай на 429/5xx и даже 4xx всё равно тратит квоту на
            # их стороне, сетевая ошибка ДО получения ответа — нет.
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
                # Не-транзиентная ошибка (неверный include, нет доступа по
                # плану, 404 и т.д.) — незачем ретраить, поднимаем сразу с
                # телом ответа, там обычно человекочитаемое message от API.
                raise SportmonksAPIError(f"HTTP {response.status_code} на {path}: {response.text[:500]}")

            payload = response.json()
            self._log_rate_limit_if_low(path, payload)
            return payload

        raise SportmonksAPIError(f"Не удалось получить {path} после {MAX_RETRIES} попыток: {last_error}")

    def _get_data(self, path: str, params: Optional[dict] = None) -> dict:
        """Обёртка над _get() для одиночных сущностей (fixture/team/league/
        player/referee/coach/...) — раньше каждый из 7 вызывающих методов
        сам индексировал `['data']` напрямую.

        ИСПРАВЛЕНО (2026-09-10, реальный краш в проде — `KeyError: 'data'`
        на первом же вызове get_player() при прогоне `fix_foreign_names
        --all`): _get() поднимает SportmonksAPIError ТОЛЬКО для HTTP-статусов
        >=400 (после ретраев транзиентных) — если Sportmonks ответил 200, но
        тело ответа почему-то не содержит "data" (не видели вживую, почему
        именно — нужен реальный текст ответа, чтобы понять: не хватает
        доступа по плану на конкретный эндпоинт, содержательная ошибка в
        обёртке 200, или что-то ещё), голый `payload['data']` падал НЕПОЙМАННЫМ
        KeyError — вызывающий код (fix_foreign_names.py и другие команды)
        ловит только SportmonksAPIError, поэтому весь прогон по 836 игрокам
        обрывался на первом же таком ответе, ничего не обработав и не
        залогировав, ПОЧЕМУ. Теперь при отсутствии "data" поднимаем
        SportmonksAPIError с ПОЛНЫМ телом ответа — вызывающий код может
        поймать её и продолжить обработку остальных записей (как и было
        задумано), а текст ошибки в логе/выводе команды покажет, что
        Sportmonks реально прислал вместо data, не оставляя гадать."""
        payload = self._get(path, params)
        if 'data' not in payload:
            raise SportmonksAPIError(
                f"Ответ {path} не содержит поля 'data' — тело ответа: {str(payload)[:500]}"
            )
        return payload['data']

    def _get_all_pages(self, path: str, params: Optional[dict] = None) -> list:
        """НАЙДЕНО ВЖИВУЮ (2026-09-09, реальный прогон sync_sportmonks_season:
        каждый из 3 запросов по датам вернул РОВНО 25 матчей — 75 на сезон
        вместо ~230-240): Sportmonks v3 — как и большинство REST API —
        пагинирует списочные эндпоинты (дефолт 25 записей на страницу,
        `pagination.has_more`/`pagination.next_page` в ответе), а `_get()`
        выше всегда возвращал только ПЕРВУЮ страницу, молча отбрасывая
        остальное. Для `/fixtures/between/...` это означало: чем шире
        диапазон дат/чем больше туров в чанке — тем больше матчей тихо
        терялось, и турнирная таблица считалась по обрезанным данным (баг,
        который заметил пользователь: "в 1 туре всего 2 матча").

        Пагинация СТРОГО последовательная (курсор `page`, не параллелим —
        не знаем общее число страниц заранее без первого ответа), но список
        для КПЛ (даже полный сезон, ~240 матчей / 25 = ~10 страниц) — это
        секунды, не риск для лимита 2000/час.

        max_pages=40 — защита от бесконечного цикла, если Sportmonks
        когда-нибудь поменяет форму пагинации и `has_more` перестанет
        когда-либо становиться False: 40 страниц × 25 = 1000 записей —
        далеко за пределами разумного для одного лигового запроса, лучше
        оборвать с warning в логе, чем зависнуть навсегда."""
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
                # Некоторые эндпоинты (одиночная сущность) отдают data как
                # dict, не list — сюда попадать не должны (вызывается только
                # для списочных методов), но на всякий случай не роняем цикл.
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
        """Sportmonks кладёт остаток лимита в каждый ответ (rate_limit.remaining,
        отдельно на каждую entity — Fixture/Team/League/... не делят один пул,
        см. докстринг модуля). При штатном использовании (bulk-опрос вместо
        цикла по матчам) расход должен быть далеко от лимита — если это не
        так, лучше узнать заранее по логам, чем поймать 429 в проде.

        ИСПРАВЛЕНО (2026-09-09, баг найден пользователем — "не увидел в
        Быстрые факты кол-во уже израсходованных запросов"): докстринг
        __init__ ВСЕГДА обещал, что self.last_rate_limit заполняется "после
        каждого успешного _get()", но метод-заглушка ниже только логировал
        warning при низком остатке и НИКОГДА фактически не присваивал
        атрибут — self.last_rate_limit оставался None весь процесс жизни
        клиента, поэтому dashboard/parser_tools.py::sportmonks_api_health_check
        всегда читал пустой словарь и блок с расходом лимита не рендерился
        вообще, даже после успешной проверки."""
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
        """Список из 16 команд лиги на сезон — участник + позиция, самый
        дешёвый способ получить полный список команд с их id разом (один
        вызов), используется реконсиляцией (parsers/management/commands/
        reconcile_sportmonks.py, docs/sportmonks-migration-plan.md фаза 2)."""
        rows = self.get_standings(season_id, include='participant')
        return [
            {'id': row['participant_id'], 'name': (row.get('participant') or {}).get('name')}
            for row in rows
            if row.get('participant')
        ]

    # -- матчи ---------------------------------------------------------------

    def get_livescores(self, include: str = 'events;participants;scores', league_id: Optional[int] = None) -> list:
        """Один вызов на ВСЮ лигу — единственный правильный способ опроса
        live-состояния, см. докстринг модуля. Возвращает пустой список, если
        сейчас нет идущих матчей вообще (нормальная ситуация большую часть
        времени, а не ошибка).

        P0 (найдено ревью Codex 2026-09-09, подтверждено по коду): у
        /livescores/inplay нет отдельного query-параметра фильтра по лиге в
        документации, как у /fixtures/between (там `filters=fixtureLeagues:`)
        — если план когда-нибудь даст токену доступ к нескольким лигам,
        сюда могут прийти чужие live-матчи. Передаём тот же `filters`
        параметр всё равно (Sportmonks игнорирует неизвестные фильтры, не
        падает 400) — это ПЕРВЫЙ барьер, ВТОРОЙ — явная проверка league_id
        каждого fx в parsers/sportmonks/tasks.py::sportmonks_update_live
        перед тяжёлой догрузкой, ТРЕТИЙ — в самом импортёре (import_match_core).

        _get_all_pages, не голый _get — в редкий момент, когда live-матчей
        одновременно больше одной страницы, обрезка страницы молча потеряла
        бы часть live-матчей."""
        league_id = league_id or settings.SPORTMONKS_LEAGUE_ID
        params = {'include': include, 'filters': f'fixtureLeagues:{league_id}'}
        return self._get_all_pages('/livescores/inplay', params)

    def get_fixtures_between(
        self, date_from: str, date_to: str, league_id: Optional[int] = None,
        include: Optional[str] = None,
    ) -> list:
        """date_from/date_to в формате YYYY-MM-DD. Основной способ забрать
        календарь/результаты сезона одним (или несколькими по диапазону)
        вызовом вместо похода за каждым матчем отдельно.

        НАЙДЕНО ВЖИВУЮ (2026-09-09, реальный бэкафилл: 75 матчей на сезон
        вместо ~230-240, "в 1 туре всего 2 матча" — см. _get_all_pages за
        полным объяснением): Sportmonks пагинирует список фикстур (дефолт
        25/страница), голый _get() отдавал только первую страницу. Теперь
        собираем ВСЕ страницы каждого date-чанка."""
        league_id = league_id or settings.SPORTMONKS_LEAGUE_ID
        params = {'filters': f'fixtureLeagues:{league_id}'}
        if include:
            params['include'] = include
        return self._get_all_pages(f"/fixtures/between/{date_from}/{date_to}", params)

    def get_fixture(self, fixture_id: int, include: Optional[str] = None) -> dict:
        """"Тяжёлый" запрос по одному матчу — вызывать только когда
        get_livescores()/get_fixtures_between() показали, что состояние
        этого конкретного матча реально изменилось (двухуровневая схема,
        см. план фаза 4), не на каждый матч каждый цикл опроса."""
        params = {'include': include} if include else None
        return self._get_data(f"/fixtures/{fixture_id}", params)

    # -- команды/составы/недоступность игроков --------------------------------

    def get_team(self, team_id: int, include: Optional[str] = None) -> dict:
        params = {'include': include} if include else None
        return self._get_data(f"/teams/{team_id}", params)

    def get_squad(self, season_id: int, team_id: int, include: str = 'player') -> list:
        # _get_all_pages — полный игровой ростер топ-клуба (25-30+ игроков
        # с резервом) может превысить дефолт 25/страница, та же причина,
        # что у get_fixtures_between выше.
        return self._get_all_pages(f"/squads/seasons/{season_id}/teams/{team_id}", {'include': include})

    def get_sidelined(self, team_id: int) -> list:
        """Дисквалификации/травмы игроков команды — новая фича, у KFF
        аналога не было (см. players.models.PlayerSidelined)."""
        return self.get_team(team_id, include='sidelined.player').get('sidelined', [])

    # -- судьи/тренеры (одиночная сущность) ------------------------------------
    # НОВОЕ (2026-09-09, parsers/management/commands/fix_foreign_names.py):
    # до сих пор не было отдельных методов на /referees/{id} и /coaches/{id}
    # — при обычном импорте имя судьи/тренера приходит вложенным объектом
    # прямо в fixture (referees[].referee, coaches[]), отдельный запрос не
    # требовался. Понадобились для точечного повторного запроса имени по
    # sportmonks_id при разовом ремонте уже испорченных записей (см. баг
    # "Слиšковиć"/"Йоãо Антóнио..." — is_likely_foreign раньше не
    # останавливал транслитерацию, см. importers.py::_resolve_cyrillic_name).
    def get_referee(self, referee_id: int) -> dict:
        return self._get_data(f"/referees/{referee_id}")

    def get_coach(self, coach_id: int) -> dict:
        return self._get_data(f"/coaches/{coach_id}")

    def get_player(self, player_id: int) -> dict:
        return self._get_data(f"/players/{player_id}")
