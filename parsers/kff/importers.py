# parsers/kff/importers.py
from datetime import datetime, timedelta
from typing import Dict, Optional, Union, List
from django.db import transaction, IntegrityError
from django.utils import timezone
from matches.models import Match, Stadium
from teams.models import Team, TeamSeason
from seasons.models import Season
from players.models import Player
from coaches.models import Coach
from lineups.models import MatchLineup, MatchLineupPlayer
from events.models import MatchEvent
from referees.models import Referee
from leagues.models import League
import logging
from django.db.models import Q

logger = logging.getLogger(__name__)

STATUS_MAP = {
    "upcoming": "scheduled",
    "scheduled": "scheduled",
    "live": "live",
    "finished": "finished",
    "postponed": "scheduled",
    "cancelled": "finished",
    "interrupted": "finished",
}

EVENT_TYPE_MAP = {
    "goal": "goal",
    "penalty_goal": "penalty",
    "own_goal": "own_goal",
    "yellow_card": "yellow_card",
    "red_card": "red_card",
    "second_yellow_card": "red_card",
    "substitution": "substitution",
    "var": "var_check",
    "missed_penalty": "missed_penalty",
    "disallowed_goal": "disallowed_goal",
    "goal_disallowed": "disallowed_goal",
    "var_disallowed": "disallowed_goal",
    "var_overturned": "disallowed_goal",
    "cancelled_goal": "disallowed_goal",
    "offside_goal": "disallowed_goal",
    "foul_goal": "disallowed_goal",
}

def parse_match_datetime(date_str: str, time_str: Optional[str], tz=None) -> datetime:
    if not date_str:
        return timezone.now()
    time_part = time_str or "19:00:00"
    try:
        dt_str = f"{date_str}T{time_part}"
        naive_dt = datetime.fromisoformat(dt_str)
        if tz is None:
            tz = timezone.get_current_timezone()
        return timezone.make_aware(naive_dt, tz)
    except (ValueError, TypeError) as e:
        logger.warning(f"⚠️ Не распарсил дату {date_str} {time_str}: {e}")
        return timezone.now()

def get_or_create_team(team_data: Dict) -> Team:
    if not team_data:
        raise ValueError("team_data is empty")
    team_ext_id = str(team_data.get("id"))
    if not team_ext_id:
        raise ValueError(f"No team id in {team_data}")
    team, created = Team.objects.update_or_create(
        external_id=team_ext_id,
        defaults={
            "name": (team_data.get("name") or "")[:255],
            "logo_url": team_data.get("logo_url") or "",
            "is_active": True,
        }
    )
    if created:
        logger.info(f"Created team: {team.name} (ID: {team_ext_id})")
    return team

def get_or_create_stadium(stadium_data: Union[str, Dict, None]) -> Optional[Stadium]:
    if not stadium_data:
        return None
    if isinstance(stadium_data, str):
        name = stadium_data.strip()[:255]
        if not name:
            return None
        stadium, _ = Stadium.objects.get_or_create(
            name=name, defaults={"city": "", "capacity": None}
        )
        return stadium
    elif isinstance(stadium_data, dict):
        ext_id = stadium_data.get("id")
        name = (stadium_data.get("name") or "Unknown")[:255]
        city = (stadium_data.get("city") or "")[:255]
        capacity = stadium_data.get("capacity")
        if ext_id:
            stadium, _ = Stadium.objects.update_or_create(
                external_id=str(ext_id),
                defaults={"name": name, "city": city, "capacity": capacity}
            )
        else:
            stadium, _ = Stadium.objects.get_or_create(
                name=name, defaults={"city": city, "capacity": capacity}
            )
        return stadium
    return None

def get_or_create_default_league() -> League:
    league, created = League.objects.get_or_create(
        external_id="pl_kz",
        defaults={"name": "Премьер-Лига Казахстан", "country": "Казахстан"}
    )
    if created:
        logger.info(f"✅ Created default league: {league.name}")
    return league

def get_or_create_season(season_id: int, league=None, season_name: str = None) -> Season:
    if league is None:
        league = get_or_create_default_league()
    year = str(season_id)
    if season_name and isinstance(season_name, str):
        import re
        match = re.search(r'(20\d{2})', season_name)
        if match:
            year = match.group(1)
    season, created = Season.objects.get_or_create(
        external_id=str(season_id),
        defaults={
            "year": year,
            "league": league,
            "is_active": True,
        }
    )
    if created:
        logger.info(f"✅ Created season: {season.year} (ID: {season_id})")
    return season

def get_or_create_referee_by_name(name: str) -> Optional[Referee]:
    if not name:
        return None
    name_parts = name.strip().split()
    if len(name_parts) >= 2:
        first_name, last_name = name_parts[0], " ".join(name_parts[1:])
    else:
        first_name, last_name = name, ""

    # Регистронезависимый поиск — KFF шлёт имя судьи произвольной строкой без
    # стабильного ID, при смене регистра ("Багдат"/"багдат") иначе завёлся бы
    # дубль-Referee. Разные буквы каз./рус. алфавита ("Сакен"/"Сэкен") этим
    # не ловятся — такие дубли объединяются вручную через админку.
    referee = Referee.objects.filter(
        Q(first_name__iexact=first_name) & Q(last_name__iexact=last_name)
    ).first()
    if referee:
        if not referee.is_active:
            referee.is_active = True
            referee.save(update_fields=["is_active"])
        return referee

    referee = Referee.objects.create(
        first_name=first_name, last_name=last_name, is_active=True
    )
    return referee

def get_or_create_coach(coach_data: Dict, team: Optional[Team] = None) -> Optional[Coach]:
    """Создание/поиск тренера по external_id, либо по имени регистронезависимо."""
    if not coach_data:
        return None
    
    # Пробуем разные форматы данных тренера
    first_name = coach_data.get("first_name") or coach_data.get("name") or ""
    last_name = coach_data.get("last_name") or ""
    coach_ext_id = coach_data.get("id")
    
    # Если имя не найдено, пробуем распарсить из полного имени
    if not first_name and coach_data.get("full_name"):
        name_parts = (coach_data.get("full_name") or "").strip().split()
        if len(name_parts) >= 2:
            first_name = name_parts[0]
            last_name = " ".join(name_parts[1:])
        elif len(name_parts) == 1:
            first_name = name_parts[0]
    
    if not first_name:
        logger.warning(f"⚠️ No coach name found in {coach_data}")
        return None
    
    defaults = {
        "first_name": first_name,
        "last_name": last_name,
        "is_active": True,
    }

    if team:
        defaults["team"] = team

    if coach_ext_id:
        defaults["external_id"] = str(coach_ext_id)

    # Ключ матчинга — external_id, если есть; иначе (first_name, last_name)
    # регистронезависимо. Матчинг по одному только first_name склеивал бы
    # разных тренеров с одинаковым именем ("Андрей", "Марат"...) в одну запись.
    if coach_ext_id:
        coach, created = Coach.objects.update_or_create(
            external_id=str(coach_ext_id),
            defaults=defaults
        )
    else:
        coach = Coach.objects.filter(
            Q(first_name__iexact=first_name) & Q(last_name__iexact=last_name)
        ).first()
        if coach:
            for field, value in defaults.items():
                setattr(coach, field, value)
            coach.save(update_fields=list(defaults.keys()))
            created = False
        else:
            coach = Coach.objects.create(**defaults)
            created = True

    if created:
        logger.info(f"✅ Created coach: {coach.first_name} {coach.last_name}")
    else:
        logger.info(f"🔄 Updated coach: {coach.first_name} {coach.last_name}")
    
    return coach

@transaction.atomic
def import_match_core(game_data: Dict, season_id: int = None) -> Match:
    logger.debug(f"Importing match {game_data.get('id')}...")
    try:
        start_time = parse_match_datetime(
            game_data.get("date"), game_data.get("time"),
            tz=timezone.get_current_timezone()
        )
        raw_status = game_data.get("status", "upcoming")
        status = STATUS_MAP.get(raw_status, "scheduled")
        home_score = game_data.get("home_score")
        away_score = game_data.get("away_score")
        end_time = voting_open_until = None
        if start_time:
            end_time = start_time + timedelta(hours=2)
            voting_open_until = start_time + timedelta(hours=48 if status == "finished" else 48)
        
        home_team_data = game_data.get("home_team", {})
        away_team_data = game_data.get("away_team", {})
        if not home_team_data or not away_team_data:
            raise ValueError(f"Missing team data for match {game_data.get('id')}")
        
        home_team = get_or_create_team(home_team_data)
        away_team = get_or_create_team(away_team_data)
        stadium = get_or_create_stadium(game_data.get("stadium"))
        
        if not season_id:
            season_id = game_data.get("season_id")
        
        season = None
        if season_id:
            season_name = game_data.get("season_name") or game_data.get("name")
            season = get_or_create_season(season_id, season_name=season_name)
        
        referee = None
        referee_name = game_data.get("referee")
        if referee_name and isinstance(referee_name, str):
            referee = get_or_create_referee_by_name(referee_name)
        
        match, created = Match.objects.update_or_create(
            external_id=str(game_data["id"]),
            defaults={
                "home_team": home_team, "away_team": away_team,
                "stadium": stadium, "start_time": start_time,
                "end_time": end_time, "status": status,
                "home_score": home_score, "away_score": away_score,
                "has_lineup": game_data.get("has_lineup", False),
                "voting_open_until": voting_open_until,
                "league": season.league if season else None,
                "season": season, "referee": referee,
            }
        )
        
        if season:
            TeamSeason.objects.get_or_create(team=home_team, season=season)
            TeamSeason.objects.get_or_create(team=away_team, season=season)
        
        logger.info(f"Match {match.id} {'created' if created else 'updated'}: {home_team} vs {away_team}")
        return match
    except IntegrityError as e:
        logger.error(f"❌ IntegrityError: {e}")
        raise
    except Exception as e:
        logger.error(f"❌ Error: {type(e).__name__}: {e}", exc_info=True)
        raise

@transaction.atomic
def import_coaches(match: Match, lineup_data: Dict) -> bool:
    """Импорт тренеров обеих команд из lineup_data."""
    if not lineup_data:
        logger.warning(f"⚠️ No lineup data for match {match.id}")
        return False
    
    coaches_data = lineup_data.get("coaches", {})
    if not coaches_data:
        logger.warning(f"⚠️ No coaches data in lineup for match {match.id}")
        return False
    
    logger.info(f"🔍 Processing coaches for match {match.id}: {coaches_data.keys()}")
    
    # Пробуем разные форматы данных
    home_coaches = coaches_data.get("home_team", []) or coaches_data.get("home", [])
    away_coaches = coaches_data.get("away_team", []) or coaches_data.get("away", [])
    
    # Если тренеры в другом формате (список с role)
    if isinstance(home_coaches, dict):
        home_coaches = [home_coaches]
    if isinstance(away_coaches, dict):
        away_coaches = [away_coaches]
    
    logger.info(f"📊 Home coaches: {len(home_coaches) if home_coaches else 0}, Away coaches: {len(away_coaches) if away_coaches else 0}")
    
    # Домашний тренер
    for coach_data in home_coaches:
        if not coach_data:
            continue
        role = (coach_data.get("role") or "").lower()
        is_main = any(kw in role for kw in ["бас бапкер", "main", "главный", "head", "manager"])
        
        if is_main or not match.home_coach:
            coach = get_or_create_coach(coach_data, match.home_team)
            if coach:
                match.home_coach = coach
                match.save(update_fields=["home_coach", "updated_at"])
                logger.info(f"✅ Linked home coach: {coach.first_name} {coach.last_name}")
                break
    
    # Гостевой тренер
    for coach_data in away_coaches:
        if not coach_data:
            continue
        role = (coach_data.get("role") or "").lower()
        is_main = any(kw in role for kw in ["бас бапкер", "main", "главный", "head", "manager"])
        
        if is_main or not match.away_coach:
            coach = get_or_create_coach(coach_data, match.away_team)
            if coach:
                match.away_coach = coach
                match.save(update_fields=["away_coach", "updated_at"])
                logger.info(f"✅ Linked away coach: {coach.first_name} {coach.last_name}")
                break
    
    return True

@transaction.atomic
def import_lineups(match: Match, lineup_data: Dict) -> bool:
    if not lineup_data:
        return False
    lineups = lineup_data.get("lineups", {})
    if not lineups:
        return False
    
    home_lineup_data = lineups.get("home_team", {}) or lineups.get("home", {})
    away_lineup_data = lineups.get("away_team", {}) or lineups.get("away", {})
    
    MatchLineup.objects.filter(match=match).delete()
    logger.info(f"🗑️ Deleted old lineups for match {match.id}")
    
    def process_team_lineup(team_side: str, lineup: Dict, team: Team):
        if not lineup:
            return
        # `or ""`, не .get(key, "") — KFF отдаёт "formation": null (не
        # отсутствие ключа), а поле в БД NOT NULL (lineups/models.py).
        formation = lineup.get("formation") or ""
        starters = lineup.get("starters", []) or lineup.get("starting_lineup", [])
        substitutes = lineup.get("substitutes", []) or lineup.get("bench", [])
        
        logger.info(f"  👥 {team_side}: {len(starters)} стартовых, {len(substitutes)} запасных")
        if len(starters) != 11:
            logger.warning(f"  ⚠️  Нестандартное количество стартовых: {len(starters)}")
        
        match_lineup, _ = MatchLineup.objects.update_or_create(
            match=match,
            team=team,
            side=team_side,
            defaults={"formation": formation}
        )
        MatchLineupPlayer.objects.filter(lineup=match_lineup).delete()
        
        def get_or_create_player(player_data: Dict, target_team: Team):
            player_ext_id = player_data.get("player_id") or player_data.get("id")
            if not player_ext_id:
                return None
            player, _ = Player.objects.update_or_create(
                external_id=str(player_ext_id),
                defaults={
                    "first_name": player_data.get("first_name", ""),
                    "last_name": player_data.get("last_name", ""),
                    "team": target_team,
                    "position": (player_data.get("amplua") or player_data.get("position", ""))[:20],
                    "number": player_data.get("shirt_number"),
                    "is_active": True,
                }
            )
            return player
        
        for player_data in starters:
            player = get_or_create_player(player_data, team)
            if not player:
                continue
            MatchLineupPlayer.objects.create(
                lineup=match_lineup,
                player=player,
                is_starting=True,
                position=(player_data.get("amplua") or player_data.get("position", ""))[:20],
                shirt_number=player_data.get("shirt_number"),
                minute_in=0,
                minute_out=None,
            )
        
        for player_data in substitutes:
            player = get_or_create_player(player_data, team)
            if not player:
                continue
            MatchLineupPlayer.objects.create(
                lineup=match_lineup,
                player=player,
                is_starting=False,
                position=(player_data.get("amplua") or player_data.get("position", ""))[:20],
                shirt_number=player_data.get("shirt_number"),
                minute_in=None,
                minute_out=None,
            )
        
        saved_starters = MatchLineupPlayer.objects.filter(
            lineup=match_lineup,
            is_starting=True
        ).count()
        logger.info(f"  ✅ Сохранено стартовых: {saved_starters}")
    
    process_team_lineup("home", home_lineup_data, match.home_team)
    process_team_lineup("away", away_lineup_data, match.away_team)
    
    total_home = MatchLineupPlayer.objects.filter(
        lineup__match=match,
        lineup__side="home",
        is_starting=True
    ).count()
    total_away = MatchLineupPlayer.objects.filter(
        lineup__match=match,
        lineup__side="away",
        is_starting=True
    ).count()
    logger.info(f"✅ Итог: Home={total_home}, Away={total_away}")
    
    if not match.has_lineup:
        match.has_lineup = True
        match.save(update_fields=["has_lineup", "updated_at"])
    return True

def find_player_by_name_in_match(match: Match, full_name: str, team_side: str, is_starting: bool = None) -> Optional[Player]:
    if not full_name:
        return None
    
    # Нормализуем имя: убираем лишние пробелы, приводим к нижнему регистру
    full_name = ' '.join(full_name.strip().split()).lower()
    name_parts = full_name.split()
    
    if not name_parts:
        return None
    
    queryset = MatchLineupPlayer.objects.filter(
        lineup__match=match,
        lineup__side=team_side,
    ).select_related('player')
    
    if is_starting is not None:
        queryset = queryset.filter(is_starting=is_starting)
    
    # 1. Точное совпадение по фамилии (последнее слово)
    if len(name_parts) >= 2:
        last_name = name_parts[-1]
        result = queryset.filter(player__last_name__iexact=last_name).first()
        if result:
            return result.player
    
    # 2. Совпадение по имени + фамилии
    if len(name_parts) >= 2:
        last_name = name_parts[-1]
        first_name = ' '.join(name_parts[:-1])
        result = queryset.filter(
            player__first_name__iexact=first_name,
            player__last_name__iexact=last_name
        ).first()
        if result:
            return result.player
    
    # 3. 🔥 НОВОЕ: Поиск по частичному совпадению (для кириллицы/латиницы)
    for part in name_parts:
        if len(part) >= 4:  # Ищем только значимые части имени
            result = queryset.filter(
                Q(player__first_name__icontains=part) |
                Q(player__last_name__icontains=part)
            ).first()
            if result:
                logger.info(f"  🔍 Found player by partial match: '{part}' -> {result.player}")
                return result.player
    
    # 4. Фоллбэк: поиск только по первому имени
    first_name = name_parts[0]
    result = queryset.filter(player__first_name__icontains=first_name).first()
    if result:
        return result.player
    
    return None

def _is_goal_disallowed(evt: Dict) -> bool:
    if evt.get("cancelled") or evt.get("disallowed") or evt.get("goal_disallowed"):
        return True
    if evt.get("var_overturned") or evt.get("var_disallowed"):
        return True
    extra = evt.get("extra_data", {}) or {}
    if extra.get("cancelled") or extra.get("disallowed") or extra.get("goal_disallowed"):
        return True
    if extra.get("var_overturned") or extra.get("var_disallowed"):
        return True
    if evt.get("status") in ["disallowed", "cancelled", "offside", "foul"]:
        return True
    if extra.get("status") in ["disallowed", "cancelled", "offside", "foul"]:
        return True
    if evt.get("valid") is False or evt.get("confirmed") is False:
        return True
    if extra.get("valid") is False or extra.get("confirmed") is False:
        return True
    event_type_raw = (evt.get("event_type") or "").lower()
    disallowed_keywords = ["disallowed", "cancelled", "offside", "foul", "var_overturn"]
    if any(kw in event_type_raw for kw in disallowed_keywords):
        return True
    return False

@transaction.atomic
def import_events_and_minutes(match: Match, events_data: Dict, replace_existing: bool = True) -> bool:
    """
    Импорт событий матча. `replace_existing=True` (полный ресинк, вызывается
    из pipeline.py::import_full_match с полным списком с API) удаляет старые
    события перед вставкой. `replace_existing=False` — только добавляет
    переданное, ничего не удаляя: используется в parsers/tasks.py::
    update_match_statuses, куда приходит только ДЕЛЬТА новых событий —
    безусловный delete+recreate на дельте стирал бы всю ранее сохранённую
    историю на каждом цикле живого опроса.
    """
    if not events_data:
        return False

    # ✅ Поддержка разных форматов: 'events' или 'data'->'events'
    events = events_data.get("events") or (
        events_data.get("data", {}).get("events") if isinstance(events_data.get("data"), dict) else []
    )

    if not events:
        return False

    if replace_existing:
        # Очищаем старые события — уместно ТОЛЬКО когда `events` содержит
        # ПОЛНЫЙ список с API (см. docstring выше), иначе см. `replace_existing=False`.
        deleted_count, _ = match.events.all().delete()
        if deleted_count > 0:
            logger.info(f"🗑️ Deleted {deleted_count} old events for match {match.id}")
    
    created_count = 0
    
    for evt in events:
        try:
            # ✅ Безопасное получение полей с дефолтами
            minute = evt.get("minute")
            if not minute:
                continue
                
            event_type_raw = (evt.get("event_type") or "").lower()
            
            # ✅ Определение команды: по team_id или team_name
            team_id = evt.get("team_id")
            team_name = evt.get("team_name", "")
            
            if team_id and match.home_team.external_id and str(team_id) == str(match.home_team.external_id):
                team_side = "home"
            elif team_name and team_name == match.home_team.name:
                team_side = "home"
            else:
                team_side = "away"
            
            # ✅ Маппинг типов событий с расширенными вариантами
            event_type = EVENT_TYPE_MAP.get(event_type_raw, event_type_raw)
            if event_type is None:
                logger.info(f"  ⚠️  Skipping unknown event type '{event_type_raw}' at {minute}'")
                continue
            
            valid_types = [et[0] for et in MatchEvent.EVENT_TYPES]
            if event_type not in valid_types:
                # Добавляем неизвестный тип если нужно
                logger.info(f"  ℹ️  Adding new event type: {event_type}")
            
            # ✅ Инициализация переменных
            player = player_out = assist_player = None
            score_after = ""
            card_reason = None
            var_decision = ""
            
            # === ОБРАБОТКА ГОЛОВ ===
            if event_type in ("goal", "penalty", "own_goal"):
                if _is_goal_disallowed(evt):
                    event_type = "disallowed_goal"
                    logger.info(f"  ⚠️  Goal at {minute}' marked as DISALLOWED")
                
                # ✅ Поиск игрока: по ID или по имени
                player_id = evt.get("player_id")
                player_name_full = (evt.get("player_name") or "").strip()
                
                if player_id:
                    player = Player.objects.filter(external_id=str(player_id)).first()
                if not player and player_name_full:
                    player = find_player_by_name_in_match(match, player_name_full, team_side)
                
                # Ассист
                assist_id = evt.get("assist_player_id") or evt.get("assist_id")
                if assist_id:
                    assist_player = Player.objects.filter(external_id=str(assist_id)).first()
                
                # Счёт после гола
                score_after = evt.get("score_after") or evt.get("home_score") and f"{evt.get('home_score')}:{evt.get('away_score')}"
                
            # === ОБРАБОТКА ЗАМЕН ===
            elif event_type == "substitution":
                # ✅ Поддержка разных форматов: player_id/player2_id или player_in_id/player_out_id
                player_out_id = evt.get("player_id") or evt.get("player_out_id")
                player_in_id = evt.get("player2_id") or evt.get("player_in_id")
                
                player_out_name = (evt.get("player_name") or "").strip() or (evt.get("player_out_name") or "").strip()
                player_in_name = (evt.get("player2_name") or "").strip() or (evt.get("player_in_name") or "").strip()
                
                logger.info(f"  🔄 {minute}' [{team_side}]: OUT={player_out_name}({player_out_id}), IN={player_in_name}({player_in_id})")
                
                # Поиск игрока, который ушёл
                if player_out_id:
                    candidate = Player.objects.filter(external_id=str(player_out_id)).first()
                    if candidate and candidate.team_id in [match.home_team_id, match.away_team_id]:
                        if MatchLineupPlayer.objects.filter(
                            lineup__match=match, lineup__side=team_side, player=candidate
                        ).exists():
                            player_out = candidate
                
                # Фоллбэк: поиск по имени
                if not player_out and player_out_name:
                    player_out = find_player_by_name_in_match(match, player_out_name, team_side, is_starting=True)
                
                # Поиск игрока, который вышел
                if player_in_id:
                    candidate = Player.objects.filter(external_id=str(player_in_id)).first()
                    if candidate and candidate.team_id in [match.home_team_id, match.away_team_id]:
                        if MatchLineupPlayer.objects.filter(
                            lineup__match=match, lineup__side=team_side, player=candidate
                        ).exists():
                            player = candidate
                
                # Фоллбэк: поиск по имени
                if not player and player_in_name:
                    player = find_player_by_name_in_match(match, player_in_name, team_side, is_starting=False)
                
                # ✅ Обновляем состав: игрок вышел на замену
                if player:
                    MatchLineupPlayer.objects.filter(
                        lineup__match=match, player=player
                    ).update(minute_in=minute, is_starting=False)
                
                # ✅ Обновляем состав: игрок ушёл с поля
                if player_out:
                    MatchLineupPlayer.objects.filter(
                        lineup__match=match, player=player_out
                    ).update(minute_out=minute)
                
                logger.info(f"  📝 {minute}': player(IN)={player}, player_out(OUT)={player_out}")
                
            # === ОБРАБОТКА КАРТОЧЕК ===
            elif event_type in ("yellow_card", "red_card"):
                player_id = evt.get("player_id")
                player_name_full = (evt.get("player_name") or "").strip()
                
                if player_id:
                    player = Player.objects.filter(external_id=str(player_id)).first()
                if not player and player_name_full:
                    player = find_player_by_name_in_match(match, player_name_full, team_side)
                
                # Причина карточки
                reason = evt.get("reason") or evt.get("card_reason")
                if reason:
                    card_reason = "unsporting" if "unsporting" in reason.lower() else "other"
            
            # === VAR ===
            elif event_type == "var_check":
                var_decision = evt.get("decision") or evt.get("var_decision", "")
            
            # === СОЗДАНИЕ СОБЫТИЯ ===
            MatchEvent.objects.create(
                match=match,
                player=player,
                minute=minute,
                # ✅ Безопасное получение добавленного времени
                added_time=evt.get("added_time") or evt.get("extra_time", 0),
                event_type=event_type,
                team_side=team_side,
                assist_player=assist_player,
                score_after=score_after,
                player_out=player_out,
                card_reason=card_reason,
                var_decision=var_decision,
                # ✅ Сохраняем сырые данные для отладки
                extra_data=evt,
            )
            created_count += 1
            
        except Exception as e:
            logger.error(f"⚠️ Ошибка события: {e} | Data: {evt}", exc_info=True)
            continue
    
    event_count = MatchEvent.objects.filter(match=match).count()
    logger.info(f"✅ Imported {created_count} events for match {match.id} (total in DB: {event_count})")
    return True

@transaction.atomic
def import_stats(match: Match, stats_data: Dict) -> bool:
    if not stats_data:
        return False
    logger.info(f"Stats imported for match {match.id}")
    return True