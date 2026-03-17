from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
from django.db import transaction, IntegrityError
from django.utils import timezone
import logging
import re

logger = logging.getLogger(__name__)

from teams.models import Team, TeamSeason
from players.models import Player
from matches.models import Match
from events.models import MatchEvent
from lineups.models import MatchLineup, MatchLineupPlayer
from coaches.models import Coach
from referees.models import Referee
from core.models_stadium import Stadium
from seasons.models import Season
from leagues.models import League


# --- Helpers ---

def get_or_create_stadium(data: Dict) -> Optional[Stadium]:
    """Создаёт или обновляет стадион по external_id"""
    if not data:
        return None
    ext_id = str(data.get("id"))
    stadium, _ = Stadium.objects.update_or_create(
        external_id=ext_id,
        defaults={
            "name": data.get("name", "Unknown")[:255],
            "city": data.get("city", "")[:255],
            "capacity": data.get("capacity"),
        }
    )
    return stadium


def get_or_create_team(data: Dict) -> Team:
    """Создаёт или обновляет команду по external_id"""
    ext_id = str(data["id"])
    team, _ = Team.objects.update_or_create(
        external_id=ext_id,
        defaults={
            "name": data.get("name", "Unknown")[:255],
            "logo_url": data.get("logo_url"),
            "city": data.get("city", "")[:120],
        }
    )
    return team


def get_or_create_player(data: Dict, team: Optional[Team] = None) -> Player:
    """Создаёт или обновляет игрока по external_id"""
    p_id = data.get("player_id") or data.get("id")
    if not p_id:
        raise ValueError("No player ID provided")
    
    ext_id = str(p_id)
    first_name = data.get("first_name", "") or ""
    last_name = data.get("last_name", "") or ""
    
    defaults = {
        "first_name": first_name[:120],
        "last_name": last_name[:120],
    }
    
    # Заполняем team, position, number если переданы
    if team:
        defaults["team"] = team
    if data.get("position") or data.get("amplua"):
        defaults["position"] = (data.get("amplua") or data.get("position") or "")[:50]
    if data.get("shirt_number") is not None:
        defaults["number"] = data.get("shirt_number")
    
    player, _ = Player.objects.update_or_create(
        external_id=ext_id,
        defaults=defaults
    )
    return player


def get_or_create_coach(data: Dict, team: Optional[Team] = None) -> Coach:
    """Создаёт или обновляет тренера с привязкой к команде"""
    ext_id = str(data["id"])
    defaults = {
        "first_name": data.get("first_name", "")[:120],
        "last_name": data.get("last_name", "")[:120],
        "is_active": True,
    }
    if team:
        defaults["team"] = team
    
    coach, _ = Coach.objects.update_or_create(
        external_id=ext_id,
        defaults=defaults
    )
    return coach


def get_or_create_referee(data: Dict) -> Referee:
    """Создаёт или обновляет судью по external_id из lineup endpoint"""
    ext_id = str(data["id"])
    ref, _ = Referee.objects.update_or_create(
        external_id=ext_id,
        defaults={
            "first_name": data.get("first_name", "")[:120],
            "last_name": data.get("last_name", "")[:120],
            "country": data.get("country", {}).get("name", "") if data.get("country") else "",
            "is_active": True,
        }
    )
    return ref


def get_or_create_referee_by_name(referee_name: str) -> Optional[Referee]:
    """
    Создаёт или находит судью по имени из game endpoint.
    Формат имени: "Бағдат Абдуллаев" или "Санжар Ысқақов"
    """
    if not referee_name:
        return None
    
    # Очищаем имя от лишних пробелов
    referee_name = referee_name.strip()
    
    # Разделяем на имя и фамилию (последнее слово - фамилия)
    parts = referee_name.split()
    if len(parts) >= 2:
        first_name = " ".join(parts[:-1])[:120]
        last_name = parts[-1][:120]
    elif len(parts) == 1:
        first_name = parts[0][:120]
        last_name = ""
    else:
        return None
    
    # Пытаемся найти существующего судью
    referee = Referee.objects.filter(
        first_name=first_name,
        last_name=last_name
    ).first()
    
    if not referee:
        # Создаём нового
        referee = Referee.objects.create(
            first_name=first_name,
            last_name=last_name,
            is_active=True
        )
        logger.info(f"Created new referee: {first_name} {last_name}")
    
    return referee


def get_or_create_season(season_id: int, season_name: str = "") -> Season:
    """Создаёт или получает сезон, привязывая к лиге"""
    ext_id = str(season_id)
    
    # Получаем или создаём лигу Премьер-Лига
    league, _ = League.objects.get_or_create(
        external_id="pl_kz",
        defaults={
            "name": "Премьер-Лига Казахстан",
            "country": "Kazakhstan",
        }
    )
    
    # Извлекаем год из названия сезона
    year = "2026"
    if season_name and "2026" in season_name:
        year = "2026"
    
    season, _ = Season.objects.update_or_create(
        external_id=ext_id,
        defaults={
            "league": league,
            "year": year,
            "is_active": True,
        }
    )
    return season


# --- Main Importers ---

@transaction.atomic
def import_match_core(game_data: Dict, season_id: int = None) -> Match:
    """
    Импортирует основную информацию о матче.
    
    Устанавливает:
    - referee (из game_data)
    - end_time (start_time + 2 часа)
    - voting_open_until (start_time + 48 часов)
    """
    home_team = get_or_create_team(game_data["home_team"])
    away_team = get_or_create_team(game_data["away_team"])
    stadium = get_or_create_stadium(game_data.get("stadium"))
    
    # Парсинг даты и времени с timezone
    date_str = game_data.get("date")
    time_str = game_data.get("time")
    start_time = None
    if date_str and time_str:
        try:
            naive_dt = datetime.fromisoformat(f"{date_str}T{time_str}")
            start_time = timezone.make_aware(naive_dt)
        except (ValueError, TypeError):
            start_time = timezone.now()
    
    # ⚽ РАСЧЁТ end_time (матч длится ~2 часа)
    end_time = None
    if start_time:
        end_time = start_time + timedelta(hours=2)
    
    # 🗳️ РАСЧЁТ voting_open_until (48 часов после начала матча)
    voting_open_until = None
    if start_time:
        voting_open_until = start_time + timedelta(hours=48)
    else:
        voting_open_until = timezone.now() + timedelta(hours=48)
    
    # Маппинг статусов
    status_map = {
        "scheduled": "scheduled",
        "live": "live",
        "finished": "finished",
        "upcoming": "scheduled",
        "postponed": "scheduled",
        "cancelled": "finished",
    }
    raw_status = game_data.get("status", "scheduled")
    status = status_map.get(raw_status, "scheduled")
    
    # Счета могут быть null
    home_score = game_data.get("home_score")
    away_score = game_data.get("away_score")
    
    # Получаем сезон (ОБЯЗАТЕЛЬНО)
    season = None
    league = None
    actual_season_id = season_id or game_data.get("season_id")
    
    if actual_season_id:
        try:
            season = get_or_create_season(actual_season_id, game_data.get("season_name", ""))
            league = season.league
        except Exception as e:
            logger.warning(f"Could not create season: {e}")
            league, _ = League.objects.get_or_create(
                external_id="pl_kz",
                defaults={"name": "Премьер-Лига Казахстан", "country": "Kazakhstan"}
            )
            season, _ = Season.objects.get_or_create(
                external_id=str(actual_season_id),
                defaults={"league": league, "year": "2026", "is_active": True}
            )
    else:
        league, _ = League.objects.get_or_create(
            external_id="pl_kz",
            defaults={"name": "Премьер-Лига Казахстан", "country": "Kazakhstan"}
        )
        season, _ = Season.objects.get_or_create(
            external_id="200",
            defaults={"league": league, "year": "2026", "is_active": True}
        )
    
    # 🟨 ОБРАБОТКА СУДЬИ из game endpoint (строка имени)
    referee = None
    referee_name = game_data.get("referee")
    if referee_name:
        try:
            referee = get_or_create_referee_by_name(referee_name)
            logger.info(f"Referee for match {game_data.get('id')}: {referee_name}")
        except Exception as e:
            logger.warning(f"Error processing referee '{referee_name}': {e}")
    
    # Создаём/обновляем матч со всеми полями
    match, created = Match.objects.update_or_create(
        external_id=str(game_data["id"]),
        defaults={
            "home_team": home_team,
            "away_team": away_team,
            "stadium": stadium,
            "start_time": start_time,
            "end_time": end_time,  # ⚽ +2 часа от start_time
            "status": status,
            "home_score": home_score,
            "away_score": away_score,
            "has_lineup": game_data.get("has_lineup", False),
            "voting_open_until": voting_open_until,  # 🗳️ +48 часов от start_time
            "league": league,
            "season": season,
            "referee": referee,  # 🟨 Судья из game endpoint
        }
    )
    
    # Привязываем команды к сезону
    TeamSeason.objects.get_or_create(team=home_team, season=season)
    TeamSeason.objects.get_or_create(team=away_team, season=season)
    
    logger.info(
        f"Match {match.id} created/updated: "
        f"start={start_time}, end={end_time}, voting_until={voting_open_until}, "
        f"referee={referee}"
    )
    
    return match


@transaction.atomic
def import_lineups(match: Match, lineup_data: Dict):
    """Импортирует составы, тренеров и судей из lineup endpoint"""
    if not lineup_data or not lineup_data.get("has_lineup"):
        return False
    
    # 1. Судьи из lineup endpoint (с ID и ролями)
    # Сохраняем только главного судью (role = "main")
    for ref_info in lineup_data.get("referees", []):
        if ref_info.get("role") == "main":
            try:
                referee = get_or_create_referee(ref_info)
                # Обновляем судью в матче если ещё не установлен
                if not match.referee:
                    match.referee = referee
                    match.save(update_fields=["referee"])
            except Exception as e:
                logger.warning(f"Error importing referee: {e}")
    
    # 2. Тренеры с привязкой к команде
    coaches = lineup_data.get("coaches", {})
    home_coaches = coaches.get("home_team", [])
    away_coaches = coaches.get("away_team", [])
    
    if home_coaches:
        head_coach = next(
            (c for c in home_coaches if "Бас бапкер" in c.get("role", "")),
            home_coaches[0]
        )
        match.home_coach = get_or_create_coach(head_coach, team=match.home_team)
    
    if away_coaches:
        head_coach = next(
            (c for c in away_coaches if "Бас бапкер" in c.get("role", "")),
            away_coaches[0]
        )
        match.away_coach = get_or_create_coach(head_coach, team=match.away_team)
    
    if match.home_coach or match.away_coach:
        match.save(update_fields=["home_coach", "away_coach"])
    
    # 3. Составы команд
    lineups_info = lineup_data.get("lineups", {})
    
    for side_key, db_side, team_obj in [
        ("home_team", "home", match.home_team),
        ("away_team", "away", match.away_team)
    ]:
        team_data = lineups_info.get(side_key)
        if not team_data:
            continue
        
        lineup_obj, _ = MatchLineup.objects.update_or_create(
            match=match,
            team=team_obj,
            side=db_side,
            defaults={"formation": team_data.get("formation") or ""}
        )
        
        # Очищаем старых игроков состава
        lineup_obj.players.all().delete()
        
        # Стартовый состав
        for p in team_data.get("starters", []):
            try:
                player = get_or_create_player(p, team=team_obj)
                MatchLineupPlayer.objects.create(
                    lineup=lineup_obj,
                    player=player,
                    is_starting=True,
                    shirt_number=p.get("shirt_number"),
                    position=(p.get("amplua") or p.get("position") or "")[:20],
                    minute_in=0,
                    minute_out=None
                )
            except Exception as e:
                logger.warning(f"Error importing starter {p}: {e}")
        
        # Запасные
        for p in team_data.get("substitutes", []):
            try:
                player = get_or_create_player(p, team=team_obj)
                MatchLineupPlayer.objects.create(
                    lineup=lineup_obj,
                    player=player,
                    is_starting=False,
                    shirt_number=p.get("shirt_number"),
                    position=(p.get("amplua") or p.get("position") or "")[:20],
                    minute_in=None,
                    minute_out=None
                )
            except Exception as e:
                logger.warning(f"Error importing substitute {p}: {e}")
    
    return True


@transaction.atomic
def import_events_and_minutes(match: Match, events_data: Dict):
    """Импортирует события и вычисляет minute_in/minute_out для замен"""
    if not events_data:
        return False
    
    events_list = events_data.get("events", [])
    if not events_list:
        return False
    
    subs_out = {}
    subs_in = {}
    
    for ev in events_list:
        ev_type = ev.get("event_type")
        minute = ev.get("minute")
        team_id = ev.get("team_id")
        
        # Определяем сторону матча
        if match.home_team.external_id and str(team_id) == str(match.home_team.external_id):
            team_side = "home"
        elif match.away_team.external_id and str(team_id) == str(match.away_team.external_id):
            team_side = "away"
        else:
            team_side = "home" if team_id == match.home_team_id else "away"
        
        player = None
        player_ext_id = str(ev["player_id"]) if ev.get("player_id") else None
        if player_ext_id:
            try:
                player = Player.objects.get(external_id=player_ext_id)
            except Player.DoesNotExist:
                player = Player.objects.create(
                    external_id=player_ext_id,
                    first_name=ev.get("player_name", "Unknown").split()[0] if ev.get("player_name") else "Unknown",
                    last_name=""
                )
        
        try:
            MatchEvent.objects.update_or_create(
                external_id=str(ev["id"]),
                defaults={
                    "match": match,
                    "player": player,
                    "minute": minute,
                    "event_type": ev_type,
                    "team_side": team_side,
                }
            )
        except IntegrityError:
            pass
        
        # Логика замен
        if ev_type == "substitution":
            player_out_id = str(ev["player_id"]) if ev.get("player_id") else None
            player_in_id = str(ev.get("player2_id")) if ev.get("player2_id") else None
            if player_out_id:
                subs_out[player_out_id] = minute
            if player_in_id:
                subs_in[player_in_id] = minute
    
    # Обновляем minute_in/minute_out
    for lineup in match.lineups.all():
        for lp in lineup.players.all():
            p_ext_id = lp.player.external_id
            if p_ext_id in subs_out:
                lp.minute_out = subs_out[p_ext_id]
            if p_ext_id in subs_in:
                lp.minute_in = subs_in[p_ext_id]
                lp.is_starting = False
            lp.save()
    
    return True


@transaction.atomic
def import_stats(match: Match, stats_data: Dict):
    """Импортирует статистику матча"""
    if not stats_data:
        return False
    # Здесь можно добавить сохранение team_stats и player_stats
    return True