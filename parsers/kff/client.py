# parsers/kff/client.py
import requests
import time
import logging
from typing import List, Dict, Optional, Any

logger = logging.getLogger(__name__)

class KFFClient:
    BASE_URL = "https://kffleague.kz"
    API_URL = "https://kffleague.kz/api/v1"
    
    # ✅ КОДЫ ТУРНИРОВ — используем frontend_code из API
    # ✅ ЛЕГКО ВКЛЮЧИТЬ: добавьте код в этот список
    TOURNAMENT_CODES = {
        'pl': 'Премьер-Лига',
        '1l': 'Первая лига',
        '2l': 'Вторая лига',
        'cup': 'Кубок Казахстана',
        'el': 'Женская лига',
        'sc': 'Суперкубок',
    }
    
    # ✅ ЦЕЛЕВОЙ ТУРНИР — по умолчанию только Премьер-Лига
    # ✅ ЧТОБЫ ВКЛЮЧИТЬ ДРУГИЕ: измените в settings.py PARSER_SETTINGS.ENABLED_TOURNAMENTS
    TARGET_TOURNAMENT = 'pl'
    
    # ✅ Известные сезон-иды для фоллбэка (только Премьер-Лига)
    KNOWN_PL_SEASON_IDS = [200, 201, 202, 203, 204, 205, 206]
    
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Referer": "https://kffleague.kz/",
            "Origin": "https://kffleague.kz",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        })
    
    def _get(self, endpoint: str, params: Optional[Dict] = None, retries: int = 3) -> Optional[Dict]:
        """GET-запрос с повторами"""
        url = f"{self.API_URL}{endpoint}"
        
        for attempt in range(retries):
            try:
                response = self.session.get(url, params=params, timeout=15)
                
                if response.status_code != 200:
                    logger.warning(f"HTTP {response.status_code} | Attempt {attempt+1} | {url}")
                    if attempt < retries - 1:
                        time.sleep(1 * (attempt + 1))
                    continue
                
                try:
                    result = response.json()
                    time.sleep(0.2)
                    return result
                except ValueError as e:
                    logger.error(f"JSON decode error: {e} | {url}")
                    return None
                    
            except requests.exceptions.Timeout:
                logger.warning(f"Timeout attempt {attempt+1} for {url}")
                if attempt < retries - 1:
                    time.sleep(2 * (attempt + 1))
            except requests.exceptions.ConnectionError as e:
                logger.warning(f"Connection error: {e}")
                if attempt < retries - 1:
                    time.sleep(2 * (attempt + 1))
            except Exception as e:
                logger.error(f"Unexpected error: {type(e).__name__}: {e}")
                break
        
        return None
    
    def get_tournament_seasons(self, tournament_code: str = None) -> List[Dict]:
        """Получает список сезонов для конкретного турнира через frontend_code"""
        if tournament_code is None:
            tournament_code = self.TARGET_TOURNAMENT
        
        seasons = []
        
        # Запрос /seasons с параметром tournament
        response = self._get("/seasons", params={"tournament": tournament_code})
        
        if not response:
            logger.warning(f"Empty response for /seasons?tournament={tournament_code}")
            return seasons
        
        # ✅ API возвращает список в ключе 'items'
        items = response.get("items", [])
        
        if not isinstance(items, list):
            logger.warning(f"Unexpected response format: {type(items)}")
            return seasons
        
        for item in items:
            if not isinstance(item, dict):
                continue
            
            # ✅ ФИЛЬТР: берём только сезоны с нужным frontend_code
            # ИСПРАВЛЕНО: .get(key, "") подставляет дефолт только если ключа
            # НЕТ вообще — если API вернул "frontend_code": null (ключ есть,
            # значение None), .get() отдаёт None, и .lower() падает. `or ""`
            # ловит оба случая — и отсутствие ключа, и null-значение.
            frontend_code = (item.get("frontend_code") or "").lower()
            if frontend_code != tournament_code.lower():
                continue
            
            season_id = item.get('id')
            if not season_id:
                continue
            
            # Извлекаем год из name
            name = item.get('name', '')
            year = None
            if name and isinstance(name, str):
                import re
                match = re.search(r'(20\d{2})', name)
                if match:
                    year = int(match.group(1))
            
            seasons.append({
                'id': int(season_id),
                'year': year,
                'name': name,
                'is_current': item.get('is_current', False),
                'frontend_code': frontend_code,
                'championship_name': item.get('championship_name', ''),
            })
        
        logger.info(f"Found {len(seasons)} seasons for tournament '{tournament_code}' (frontend_code filter applied)")
        return seasons
    
    def find_premier_league_season(self, year: int = None, prefer_current: bool = True) -> Optional[int]:
        """Находит сезон Премьер-Лиги через frontend_code='pl'"""
        # ✅ Получаем сезоны ТОЛЬКО для Премьер-Лиги (frontend_code='pl')
        seasons = self.get_tournament_seasons(tournament_code=self.TARGET_TOURNAMENT)
        
        if not seasons:
            logger.warning(f"No seasons found for tournament '{self.TARGET_TOURNAMENT}' via API")
            # Фоллбэк: пробуем известные сезон-иды для Премьер-Лиги
            for sid in self.KNOWN_PL_SEASON_IDS:
                test_resp = self._get(f"/seasons/{sid}")
                if test_resp and (test_resp.get("frontend_code") or "").lower() == self.TARGET_TOURNAMENT:
                    logger.info(f"✅ Using fallback Premier League season ID: {sid}")
                    return sid
            
            logger.error(f"❌ Could not find any Premier League season (frontend_code={self.TARGET_TOURNAMENT})")
            return None
        
        candidates = []
        for s in seasons:
            season_id = s.get('id')
            season_year = s.get('year')
            is_current = s.get('is_current', False)
            
            if not season_id:
                continue
            
            # Фильтр по году если указан
            if year and season_year != year:
                continue
            
            # Приоритет: is_current > по году
            priority = 0
            if prefer_current and is_current:
                priority = 1000
            if season_year:
                priority += season_year
            
            candidates.append({
                'season_id': season_id,
                'year': season_year,
                'is_current': is_current,
                'priority': priority,
                'name': s.get('name', ''),
            })
        
        # Сортировка по приоритету
        candidates.sort(key=lambda x: (-x['priority'], -(x['year'] or 0)))
        
        if candidates:
            selected = candidates[0]
            logger.info(
                f"✅ Selected Premier League season: {selected['season_id']} "
                f"(year={selected['year']}, current={selected['is_current']}, name='{selected['name']}')"
            )
            return selected['season_id']
        
        logger.error("❌ Could not select Premier League season")
        return None
    
    def get_season_matches(self, season_id: int = None, tournament_code: str = None, auto_detect: bool = True) -> List[int]:
        """
        Получает список ID матчей сезона
        Returns:
            Список ID: [1100, 1099, 1091, ...]
        """
        if tournament_code is None:
            tournament_code = self.TARGET_TOURNAMENT
        
        # Авто-определение сезона если не указан
        if season_id is None and auto_detect:
            season_id = self.find_premier_league_season()
            if not season_id:
                logger.error("❌ Could not auto-detect Premier League season")
                return []
        
        match_ids = []  # ✅ Возвращаем список ID, а не словари
        
        # === Способ 1: Прямой /seasons/{id}/games ===
        # ✅ API возвращает матчи в ключе 'items'
        games_resp = self._get(f"/seasons/{season_id}/games", params={"tournament": tournament_code})
        if games_resp:
            items = games_resp.get("items", [])
            if isinstance(items, list):
                for game in items:
                    if isinstance(game, dict) and game.get("id"):
                        match_ids.append(game["id"])  # ✅ Только ID
            
            logger.info(f"✅ Found {len(match_ids)} match IDs via /seasons/{season_id}/games")
        
        # === Способ 2: stages -> games (fallback) ===
        if not match_ids:
            stages_resp = self._get(f"/seasons/{season_id}", params={"tournament": tournament_code})
            if stages_resp:
                stages = stages_resp.get("stages", []) or stages_resp.get("rounds", [])
                for stage in stages:
                    stage_id = stage.get("id")
                    if stage_id:
                        stage_games = self._get(f"/stages/{stage_id}/games", params={"tournament": tournament_code})
                        if stage_games:
                            items = stage_games.get("items", [])
                            if isinstance(items, list):
                                for game in items:
                                    if isinstance(game, dict) and game.get("id"):
                                        match_ids.append(game["id"])
                
                if match_ids:
                    logger.info(f"✅ Found {len(match_ids)} match IDs via stages fallback")
        
        # === Способ 3: HTML scraping fallback ===
        if not match_ids:
            match_ids = list(self._scrape_match_ids_from_html(season_id, tournament_code))
            logger.info(f"✅ Found {len(match_ids)} match IDs for season {season_id} (tournament={tournament_code})")
        
        return match_ids  # ✅ Возвращаем [1100, 1099, ...], а не [{'id': 1100, ...}]
    
    def get_recent_finished_matches(self, season_id: int = None, limit: int = 10, tournament_code: str = None) -> List[int]:
        """
        Получает последние N завершённых матчей (сортировка по дате)
        Возвращает список ID
        """
        if tournament_code is None:
            tournament_code = self.TARGET_TOURNAMENT
        
        # Получаем матчи с деталями для фильтрации
        all_matches = self._get_season_matches_with_details(season_id, tournament_code)
        
        if not all_matches:
            return []
        
        # ✅ Фильтруем только завершённые матчи
        finished = [m for m in all_matches if m.get('status') == 'finished']
        
        if not finished:
            logger.warning(f"No finished matches found for season {season_id}")
            return []
        
        # ✅ Сортируем по дате (новые сначала)
        from datetime import datetime
        def parse_date(m):
            dt = m.get('start_time') or m.get('date')
            if not dt:
                return datetime.min
            try:
                if 'T' in str(dt):
                    return datetime.fromisoformat(str(dt).replace('Z', '+00:00'))
                return datetime.strptime(str(dt), '%Y-%m-%d')
            except:
                return datetime.min
        
        finished.sort(key=parse_date, reverse=True)
        
        # ✅ Берём последние N завершённых
        recent = finished[:limit]
        recent_ids = [m['id'] for m in recent]
        
        logger.info(f"✅ Selected {len(recent_ids)} recent finished matches: {recent_ids}")
        return recent_ids
    
    def _get_season_matches_with_details(self, season_id: int, tournament_code: str = None) -> List[Dict]:
        """Внутренний метод: получает матчи с деталями для фильтрации"""
        if tournament_code is None:
            tournament_code = self.TARGET_TOURNAMENT
        
        matches = []
        
        # === Способ 1: Прямой /seasons/{id}/games ===
        games_resp = self._get(f"/seasons/{season_id}/games", params={"tournament": tournament_code})
        if games_resp:
            items = games_resp.get("items", [])
            if isinstance(items, list):
                for game in items:
                    if isinstance(game, dict) and game.get("id"):
                        matches.append({
                            'id': game["id"],
                            'start_time': game.get("date") or game.get("start_time"),
                            'status': game.get("status", "scheduled"),
                        })
            
            logger.info(f"✅ Found {len(matches)} matches with details via /seasons/{season_id}/games")
        
        # === Способ 2: stages -> games (fallback) ===
        if not matches:
            stages_resp = self._get(f"/seasons/{season_id}", params={"tournament": tournament_code})
            if stages_resp:
                stages = stages_resp.get("stages", []) or stages_resp.get("rounds", [])
                for stage in stages:
                    stage_id = stage.get("id")
                    if stage_id:
                        stage_games = self._get(f"/stages/{stage_id}/games", params={"tournament": tournament_code})
                        if stage_games:
                            items = stage_games.get("items", [])
                            if isinstance(items, list):
                                for game in items:
                                    if isinstance(game, dict) and game.get("id"):
                                        matches.append({
                                            'id': game["id"],
                                            'start_time': game.get("date") or game.get("start_time"),
                                            'status': game.get("status", "scheduled"),
                                        })
            
            if matches:
                logger.info(f"✅ Found {len(matches)} matches with details via stages fallback")
        
        return matches
    
    def _scrape_match_ids_from_html(self, season_id: int, tournament_code: str = None) -> set:
        """Fallback: парсинг HTML с параметром tournament"""
        import re
        
        if tournament_code is None:
            tournament_code = self.TARGET_TOURNAMENT
        
        match_ids = set()
        
        try:
            response = self.session.get(
                f"{self.BASE_URL}/matches",
                params={"tournament": tournament_code, "season": season_id},
                timeout=10
            )
            pattern = r'(?:matches|games)[/"\\]+(\d+)'
            found = re.findall(pattern, response.text)
            match_ids.update(int(x) for x in found if x.isdigit())
            logger.info(f"🔍 Scraped {len(match_ids)} match IDs from HTML (tournament={tournament_code})")
        except Exception as e:
            logger.warning(f"⚠️ HTML scraping failed: {e}")
        
        return match_ids
    
    def get_game_details(self, match_id: int, tournament_code: str = None) -> Optional[Dict]:
        if tournament_code is None:
            tournament_code = self.TARGET_TOURNAMENT
        return self._get(f"/games/{match_id}", params={"lang": "kz", "tournament": tournament_code})
    
    def get_lineup(self, match_id: int, tournament_code: str = None) -> Optional[Dict]:
        if tournament_code is None:
            tournament_code = self.TARGET_TOURNAMENT
        return self._get(f"/games/{match_id}/lineup", params={"lang": "kz", "tournament": tournament_code})
    
    def get_events(self, match_id: int, tournament_code: str = None) -> Optional[Dict]:
        if tournament_code is None:
            tournament_code = self.TARGET_TOURNAMENT
        return self._get(f"/live/events/{match_id}", params={"lang": "kz", "tournament": tournament_code})
    
    def get_stats(self, match_id: int, tournament_code: str = None) -> Optional[Dict]:
        if tournament_code is None:
            tournament_code = self.TARGET_TOURNAMENT
        return self._get(f"/games/{match_id}/stats", params={"lang": "kz", "tournament": tournament_code})
    
    def get_season_info(self, season_id: int, tournament_code: str = None) -> Optional[Dict]:
        if tournament_code is None:
            tournament_code = self.TARGET_TOURNAMENT
        return self._get(f"/seasons/{season_id}", params={"tournament": tournament_code})