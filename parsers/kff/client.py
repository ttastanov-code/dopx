import requests
import time
import logging
from typing import List, Dict, Optional, Any

logger = logging.getLogger(__name__)


class KFFClient:
    BASE_URL = "https://kffleague.kz"
    API_URL = "https://kffleague.kz/api/v1"
    
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
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
        """GET-запрос с повторами и логированием"""
        url = f"{self.API_URL}{endpoint}"
        
        for attempt in range(retries):
            try:
                response = self.session.get(url, params=params, timeout=15)
                
                if response.status_code != 200:
                    logger.warning(f"HTTP {response.status_code} | Attempt {attempt+1} | URL: {url}")
                    if attempt < retries - 1:
                        time.sleep(1 * (attempt + 1))
                        continue
                    return None
                
                try:
                    result = response.json()
                    time.sleep(0.2)  # Rate limiting
                    return result
                except ValueError as e:
                    logger.error(f"JSON decode error: {e} | URL: {url}")
                    return None
                    
            except requests.exceptions.Timeout:
                logger.warning(f"Timeout on attempt {attempt+1} for {url}")
                if attempt < retries - 1:
                    time.sleep(2 * (attempt + 1))
                    continue
            except requests.exceptions.ConnectionError as e:
                logger.warning(f"Connection error on attempt {attempt+1}: {e}")
                if attempt < retries - 1:
                    time.sleep(2 * (attempt + 1))
                    continue
            except Exception as e:
                logger.error(f"Unexpected error: {type(e).__name__}: {e} | URL: {url}")
                break
        
        return None
    
    def get_season_matches(self, season_id: int = 200) -> List[int]:
        """Получает список ID матчей для сезона"""
        match_ids = set()
        
        # === Способ 1: stages -> games ===
        stages_data = self._get(f"/seasons/{season_id}")
        if stages_data:
            data = stages_data.get("data", stages_data) if isinstance(stages_data, dict) else stages_data
            if isinstance(data, dict):
                stages = data.get("stages", [])
                for stage in stages:
                    stage_id = stage.get("id")
                    if stage_id:
                        stage_matches = self._get(f"/stages/{stage_id}/games")
                        if stage_matches:
                            games_data = stage_matches.get("data", stage_matches) if isinstance(stage_matches, dict) else stage_matches
                            if isinstance(games_data, list):
                                for game in games_data:
                                    if isinstance(game, dict) and game.get("id"):
                                        match_ids.add(game["id"])
        
        # === Способ 2: Прямой endpoint /seasons/{id}/games ===
        if not match_ids:
            games_data = self._get(f"/seasons/{season_id}/games")
            if games_data:
                data = games_data.get("data", games_data) if isinstance(games_data, dict) else games_data
                
                if isinstance(data, list):
                    # Прямой список
                    for game in data:
                        if isinstance(game, dict) and game.get("id"):
                            match_ids.add(game["id"])
                
                elif isinstance(data, dict):
                    # ✅ FIX: Проверяем ключ 'items' (новый формат API)
                    if "items" in data and isinstance(data["items"], list):
                        for game in data["items"]:
                            if isinstance(game, dict) and game.get("id"):
                                match_ids.add(game["id"])
                    # Старый формат
                    elif "games" in data and isinstance(data["games"], list):
                        for game in data["games"]:
                            if isinstance(game, dict) and game.get("id"):
                                match_ids.add(game["id"])
        
        # === Способ 3: HTML scraping fallback ===
        if not match_ids:
            match_ids = self._scrape_match_ids_from_html(season_id)
        
        # === Способ 4: Хардкод — ЗАКОММЕНТИРОВАТЬ ДЛЯ ПРОДАКШЕНА ===
        # ❌ УДАЛИТЕ или закомментируйте этот блок, иначе всегда будет 10 матчей:
        # if not match_ids and season_id == 200:
        #     logger.info("Using fallback match IDs for season 200 (test mode)")
        #     match_ids = {894, 895, 896, 897, 898, 899, 900, 901, 902, 903}
        
        logger.info(f"Found {len(match_ids)} match IDs for season {season_id}")
        return list(match_ids)
    
    def _scrape_match_ids_from_html(self, season_id: int) -> set:
        """Fallback: парсит HTML для извлечения ID матчей"""
        import re
        match_ids = set()
        try:
            response = self.session.get(
                f"{self.BASE_URL}/matches",
                params={"tournament": "pl", "season": season_id},
                timeout=10
            )
            pattern = r'(?:matches|games)[/"\\]+(\d+)'
            found = re.findall(pattern, response.text)
            match_ids.update(int(x) for x in found if x.isdigit())
            logger.info(f"Scraped {len(match_ids)} match IDs from HTML")
        except Exception as e:
            logger.warning(f"HTML scraping failed: {e}")
        return match_ids
    
    def get_game_details(self, match_id: int) -> Optional[Dict]:
        """Получает детали матча - возвращает данные напрямую"""
        return self._get(f"/games/{match_id}", {"lang": "kz"})
    
    def get_lineup(self, match_id: int) -> Optional[Dict]:
        return self._get(f"/games/{match_id}/lineup", {"lang": "kz"})
    
    def get_events(self, match_id: int) -> Optional[Dict]:
        return self._get(f"/live/events/{match_id}", {"lang": "kz"})
    
    def get_stats(self, match_id: int) -> Optional[Dict]:
        return self._get(f"/games/{match_id}/stats", {"lang": "kz"})
    
    def get_season_info(self, season_id: int) -> Optional[Dict]:
        return self._get(f"/seasons/{season_id}")