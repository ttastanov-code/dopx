# parsers/sportmonks/importers.py
"""Импорт данных Sportmonks в модели проекта.

Все сущности создаются/обновляются через update_or_create по sportmonks_id.
Для логики используем type.developer_name (стабильный код), а не локализованный
type.name — русские названия от API машинные и ненадёжные.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from datetime import timedelta
from datetime import timezone as dt_timezone
from typing import Dict, List, Optional, Tuple

from django.db import transaction
from django.utils import timezone

from coaches.models import Coach
from events.models import MatchEvent
from leagues.models import League
from lineups.models import MatchLineup, MatchLineupPlayer
from matches.models import Match, MatchPlayerStatistics, MatchTeamStatistics
from players.models import (
    Player, PlayerSidelined, PlayerSportmonksAlias, PotentialDuplicatePlayer, player_by_sportmonks_id,
)
from core.bulk import is_quiet
from core.utils import normalize_kz
from core.models import (
    NAME_SOURCE_AI_VERIFIED,
    NAME_SOURCE_CLEAN_SOURCE,
    NAME_SOURCE_GUESSED_TRANSLITERATION,
    NAME_SOURCE_LATIN_FOREIGN,
)
from parsers.models import ParserDiscrepancy, get_confirmed_corrections
from parsers.sportmonks.name_translations import PLAYER_NAME_CORRECTIONS
from parsers.sportmonks.translit import is_likely_foreign, transliterate_name
from referees.models import Referee
from seasons.models import Season
from teams.models import Team, TeamSeason

logger = logging.getLogger(__name__)


# Полный include для тяжёлого запроса одного матча (только при реальном изменении матча).
HEAVY_FIXTURE_INCLUDE = (
    # detailedPosition — именно в camelCase: в нижнем регистре API молча не отдаёт связь.
    "state;round;participants;formations;scores;"
    "lineups.type;lineups.player;lineups.details.type;lineups.detailedPosition;"
    "events.type;events.player;"
    "statistics.type;referees.type;referees.referee;coaches"
)

# --- позиции игрока ---
# Грубая позиция по position_id — fallback, если нет детальной.
POSITION_ID_MAP = {24: "GK", 25: "D", 26: "M", 27: "F"}

# Детальная позиция игрока в конкретном матче (include lineups.detailedPosition).
# 163 (Secondary Striker) маппим в CF — отдельного кода у нас нет.
DETAILED_POSITION_ID_MAP = {
    148: "CB",   # Centre Back
    149: "DM",   # Defensive Midfield
    150: "AM",   # Attacking Midfield
    151: "CF",   # Centre Forward
    152: "LW",   # Left Wing
    153: "CM",   # Central Midfield
    154: "RB",   # Right Back
    155: "LB",   # Left Back
    156: "RW",   # Right Wing
    157: "LM",   # Left Midfield
    158: "RM",   # Right Midfield
    163: "CF",  # Secondary Striker — ближе всего к CF
}

# --- статусы матча ---
# Матчим по state.developer_name, а не по числовому state_id.
STATE_MAP = {
    "NS": "scheduled",
    "TBA": "scheduled",
    "DELAYED": "scheduled",
    "INPLAY_1ST_HALF": "live",
    "HT": "live",
    "INPLAY_2ND_HALF": "live",
    "INPLAY_ET": "live",
    "EXTRA_TIME_BREAK": "live",
    "INPLAY_PENALTIES": "live",
    "BREAK": "live",
    "SUSPENDED": "live",
    "INTERRUPTED": "live",
    "PEN_BREAK": "live",
    "PENDING": "scheduled",
    "FT": "finished",
    "AET": "finished",
    "FT_PEN": "finished",
    "AWARDED": "finished",
    "WO": "finished",
    # Прерван насовсем: по регламенту переигрывают или присуждают техпобеду (придёт AWARDED) — результата пока нет.
    "ABANDONED": "postponed",
    "POSTPONED": "postponed",
    "CANCELLED": "cancelled",
    "DELETED": "cancelled",
}
# AWAITING_UPDATES и любые неизвестные состояния не меняют статус уже известного матча.

# --- бригада судей ---
REFEREE_TYPE_MAIN = 6
REFEREE_CREW_LABELS = {7: "assistant_1", 8: "assistant_2", 9: "fourth_official"}

# --- расстановка/скамейка ---------------------------------------------------
LINEUP_TYPE_STARTING = 11
LINEUP_TYPE_BENCH = 12

# --- выбор кириллического имени ---
# У Sportmonks нет одного надёжного поля с переводом: у разных игроков кириллица
# лежит то в name, то в firstname/lastname. Перебираем кандидатов и берём первый
# полностью кириллический. Известные ошибки перевода правятся отдельно
# (_apply_known_name_corrections).
_CLEAN_CYRILLIC_RE = re.compile(
    r"^[А-ЯЁа-яёӘәҒғҚқҢңӨөҰұҮүҺһІіЇїЄєЎў\s\-'`\.]+$"
)


def _is_clean_cyrillic(text: Optional[str]) -> bool:
    text = (text or "").strip()
    return bool(text) and bool(_CLEAN_CYRILLIC_RE.match(text))


def _split_name(full_name: str) -> Tuple[str, str]:
    parts = full_name.split()
    if len(parts) > 1:
        return parts[0], " ".join(parts[1:])
    return full_name, ""


def _apply_known_name_corrections(
    first_name: str, last_name: str, entity_label: str, entity_id,
) -> Tuple[str, str]:
    """Правит известные ошибки перевода в итоговом ФИО.

    Сначала ConfirmedNameCorrection (подтверждено в очереди «Проверка ФИО»),
    затем статический PLAYER_NAME_CORRECTIONS. Сверяем итоговую кириллицу, а не
    исходную латиницу: Sportmonks часто сразу присылает (неверную) кириллицу.
    """
    db_corrections = get_confirmed_corrections()
    corrected_first = (
        db_corrections.get(first_name.strip().lower())
        or PLAYER_NAME_CORRECTIONS.get(first_name.strip().lower())
    ) if first_name else None
    corrected_last = (
        db_corrections.get(last_name.strip().lower())
        or PLAYER_NAME_CORRECTIONS.get(last_name.strip().lower())
    ) if last_name else None
    if corrected_first is None and corrected_last is None:
        return first_name, last_name

    result_first = corrected_first or first_name
    result_last = corrected_last or last_name
    logger.info(
        "Sportmonks: имя %s sportmonks_id=%s исправлено известной ручной "
        "поправкой (ConfirmedNameCorrection/PLAYER_NAME_CORRECTIONS) — было "
        "%r %r, стало %r %r",
        entity_label, entity_id, first_name, last_name, result_first, result_last,
    )
    return result_first, result_last


def _resolve_cyrillic_name(entity_data: Dict, entity_label: str) -> Tuple[str, str, str]:
    """Возвращает (first_name, last_name, name_source).

    Берёт первого полностью кириллического кандидата: firstname+lastname, name,
    display_name. Иначе — транслитерация или латиница как есть.
    name_source показывает, откуда взялось ФИО (по нему verify_names_with_ai
    находит угаданные имена).
    """
    entity_id = entity_data.get("id")
    firstname = (entity_data.get("firstname") or "").strip()
    lastname = (entity_data.get("lastname") or "").strip()

    def _corrected(first_name: str, last_name: str, name_source: str) -> Tuple[str, str, str]:
        corrected_first, corrected_last = _apply_known_name_corrections(first_name, last_name, entity_label, entity_id)
        # Поправка сработала — считаем имя проверенным.
        if (corrected_first, corrected_last) != (first_name, last_name):
            name_source = NAME_SOURCE_AI_VERIFIED
        return corrected_first, corrected_last, name_source

    if _is_clean_cyrillic(firstname) and _is_clean_cyrillic(lastname):
        return _corrected(firstname, lastname, NAME_SOURCE_CLEAN_SOURCE)

    name = (entity_data.get("name") or "").strip()
    if _is_clean_cyrillic(name):
        return _corrected(*_split_name(name), NAME_SOURCE_CLEAN_SOURCE)

    display_name = (entity_data.get("display_name") or "").strip()
    # «Х. Фамилия» (инициал) не подходит как first_name.
    if _is_clean_cyrillic(display_name) and not re.match(r"^[А-ЯЁ]\.\s", display_name):
        return _corrected(*_split_name(display_name), NAME_SOURCE_CLEAN_SOURCE)

    # Чистой кириллицы нет — транслитерируем, предпочитая firstname+lastname.
    has_split_source = bool(firstname or lastname)
    latin_source = f"{firstname} {lastname}".strip() if has_split_source else (name or display_name)
    if not latin_source:
        return "", "", ""

    # Если есть оба поля firstname/lastname — используем их границу как есть.
    # Склейка и повторное разбиение ломали составные имена из нескольких слов.
    if is_likely_foreign(latin_source):
        result_first, result_last = (firstname, lastname) if has_split_source else _split_name(latin_source)
        # Явно не славянское имя с диакритикой не транслитерируем — оставляем латиницу,
        # иначе получается смесь кириллицы и латинских букв.
        logger.info(
            "Sportmonks: имя %s sportmonks_id=%s похоже на не-славянское — "
            "оставлено латиницей как есть %r %r вместо частичной/ломаной "
            "транслитерации (источник: name=%r, firstname=%r, lastname=%r, "
            "display_name=%r); практическую транскрипцию кириллицей можно "
            "выставить вручную в админке",
            entity_label, entity_data.get("id"), result_first, result_last, name, firstname, lastname, display_name,
        )
        return _corrected(result_first, result_last, NAME_SOURCE_LATIN_FOREIGN)

    if has_split_source:
        result_first, result_last = transliterate_name(firstname), transliterate_name(lastname)
    else:
        result_first, result_last = _split_name(transliterate_name(latin_source))
    logger.info(
        "Sportmonks: имя %s sportmonks_id=%s автоматически транслитерировано "
        "%r %r -> %r %r — источник (name=%r, firstname=%r, lastname=%r, "
        "display_name=%r) не дал чистой кириллицы ни по одному полю; при "
        "необходимости поправить вручную в админке",
        entity_label, entity_data.get("id"), firstname, lastname, result_first, result_last,
        name, firstname, lastname, display_name,
    )
    return _corrected(result_first, result_last, NAME_SOURCE_GUESSED_TRANSLITERATION)


# Зона L/C/R/LC/RC по числу игроков в строке formation_field — эвристика, API её не отдаёт.
_ZONE_BY_ROW_SIZE: Dict[int, List[str]] = {
    1: ["C"],
    2: ["L", "R"],
    3: ["L", "C", "R"],
    4: ["L", "LC", "RC", "R"],
    5: ["L", "LC", "C", "RC", "R"],
}

# --- события матча ---
# Подтверждены вживую: GOAL, PENALTY, SUBSTITUTION, YELLOWCARD, остальные — по документации.
EVENT_DEV_NAME_MAP = {
    "GOAL": "goal",
    "OWNGOAL": "own_goal",
    "OWN_GOAL": "own_goal",
    "PENALTY": "penalty",
    "PENALTY_MISSED": "missed_penalty",
    "MISSED_PENALTY": "missed_penalty",
    "SUBSTITUTION": "substitution",
    "YELLOWCARD": "yellow_card",
    "REDCARD": "red_card",
    "YELLOWREDCARD": "red_card",
    "VAR": "var_check",
    "VARCARD": "var_check",
    # Отменённый VAR-гол приходит отдельным событием GOAL_DISALLOWED.
    # GOAL_UNDER_REVIEW (идёт проверка) не мапим — это промежуточное состояние.
    "GOAL_DISALLOWED": "disallowed_goal",
}

# --- статистика матча ---
# developer_name -> колонка MatchTeamStatistics. Всё пришедшее целиком сохраняется в raw.
TEAM_STAT_DEV_NAME_MAP = {
    "CORNERS": "corners",
    "SHOTS_TOTAL": "shots",
    "SHOTS_ON_TARGET": "shots_on_goal",
    "HIT_WOODWORK": "shots_on_bar",
    "SHOTS_BLOCKED": "shots_blocked",
    "OFFSIDES": "offsides",
    "FOULS": "fouls",
    "YELLOWCARDS": "yellow_cards",
    "REDCARDS": "red_cards",
    "PENALTIES": "penalties",
    "SAVES": "saves",
    "BALL_POSSESSION": "possession_percent",
    "PASSES": "passes",
    "SUCCESSFUL_PASSES_PERCENTAGE": "pass_accuracy",
    "TOTAL_CROSSES": "crosses",
    "KEY_PASSES": "key_passes",
    # Опасные атаки — используются как сигнал давления команды.
    "DANGEROUS_ATTACKS": "dangerous_attacks",
}

# Статистика игроков приходит внутри lineups[].details.
PLAYER_STAT_DEV_NAME_MAP = {
    "FOULS": "fouls",
    "SAVES": "saves",
    "SHOTS_TOTAL": "shots",
    "SHOTS_ON_TARGET": "shots_on_target",
    "SHOTS_OFF_TARGET": "shots_missed",
    "HIT_WOODWORK": "shots_on_bar",
    "SHOTS_BLOCKED": "shots_blocked",
    "OFFSIDES": "offsides",
    "PENALTIES_SCORED": "penalties",
    "PENALTIES_MISSED": "missed_penalty",
    # TOUCHES — ближайший аналог possessions.
    "TOUCHES": "possessions",
}


# ============================================================================
# Справочные сущности
# ============================================================================

def get_or_create_league(league_data: Dict) -> League:
    sm_id = str(league_data["id"])
    league, created = League.objects.update_or_create(
        sportmonks_id=sm_id,
        defaults={
            "name": league_data.get("name") or "Премьер-Лига Казахстан",
            "country": "Казахстан",
            # У проекта одна лига (КПЛ); save() сам снимает флаг с остальных лиг.
            "is_primary": True,
        },
    )
    if created:
        logger.info("Sportmonks: создана лига %s (sportmonks_id=%s)", league.name, sm_id)
    return league


def get_or_create_season(
    season_data: Dict, league: League, is_current: Optional[bool] = None
) -> Season:
    sm_id = str(season_data["id"])
    name = season_data.get("name") or str(season_data.get("id"))
    year_match = re.search(r"(20\d{2})", name)
    year = year_match.group(1) if year_match else name

    defaults = {"league": league, "year": year}
    if is_current is not None:
        defaults["is_active"] = is_current

    season, created = Season.objects.update_or_create(sportmonks_id=sm_id, defaults=defaults)
    if created:
        logger.info("Sportmonks: создан сезон %s (sportmonks_id=%s)", season.year, sm_id)
    return season


def get_or_create_team(team_data: Dict) -> Team:
    """team_data — элемент fixture.participants или ответ /teams/{id}.

    logo_url полностью управляется Sportmonks и перезаписывается при каждом синке.
    Свой герб загружается в поле logo — его импорт не трогает.
    """
    sm_id = str(team_data["id"])
    team = Team.objects.filter(sportmonks_id=sm_id).first()

    if team is None:
        team = Team.objects.create(
            sportmonks_id=sm_id,
            name=(team_data.get("name") or "")[:255],
            logo_url=team_data.get("image_path") or "",
            is_active=True,
        )
        logger.info("Sportmonks: создана команда %s (sportmonks_id=%s)", team.name, sm_id)
        return team

    update_fields = []
    new_name = (team_data.get("name") or "")[:255]
    if new_name and team.name != new_name:
        team.name = new_name
        update_fields.append("name")
    new_logo_url = team_data.get("image_path") or ""
    if new_logo_url and team.logo_url != new_logo_url:
        team.logo_url = new_logo_url
        update_fields.append("logo_url")
    if not team.is_active:
        team.is_active = True
        update_fields.append("is_active")
    if update_fields:
        team.save(update_fields=update_fields + ["updated_at"])
    return team



def sportmonks_photo_url(entity_data: Optional[Dict]) -> str:
    """image_path, если это настоящее фото, а не заглушка Sportmonks."""
    path = (entity_data or {}).get("image_path") or ""
    return "" if not path or "placeholder" in path else path


def _sync_photo_url(obj, entity_data: Optional[Dict], update_fields: list) -> None:
    # Только реальное фото: заглушка в ответе не стирает уже известное.
    url = sportmonks_photo_url(entity_data)
    if url and obj.photo_url != url:
        obj.photo_url = url
        update_fields.append("photo_url")


def get_or_create_referee(referee_data: Optional[Dict]) -> Optional[Referee]:
    """Судья матчится по sportmonks_id.

    Sportmonks не переводит имена судей. Латиница пишется только при создании,
    ручную кириллическую правку в админке повторный синк не затирает.
    """
    if not referee_data:
        return None
    sm_id = referee_data.get("id")
    if sm_id is None:
        return None

    referee = Referee.objects.filter(sportmonks_id=str(sm_id)).first()
    if referee is not None:
        update_fields = []
        if not referee.is_active:
            referee.is_active = True
            update_fields.append("is_active")
        _sync_photo_url(referee, referee_data, update_fields)
        if update_fields:
            referee.save(update_fields=update_fields + ["updated_at"])
        return referee

    first_name, last_name, name_source = _resolve_cyrillic_name(referee_data, "судьи")
    referee = Referee.objects.create(
        sportmonks_id=str(sm_id), first_name=first_name, last_name=last_name, is_active=True,
        name_source=name_source, photo_url=sportmonks_photo_url(referee_data),
    )
    logger.info("Sportmonks: создан судья %s (sportmonks_id=%s)", referee.full_name, sm_id)
    return referee


def get_or_create_coach(coach_data: Optional[Dict], team: Optional[Team] = None) -> Optional[Coach]:
    """Тренер. Имена Sportmonks не переводит: пишем только при создании,
    ручную правку не затираем. Команда синкается каждый раз.
    """
    if not coach_data:
        return None
    sm_id = coach_data.get("id")
    if sm_id is None:
        return None

    coach = Coach.objects.filter(sportmonks_id=str(sm_id)).first()
    if coach is not None:
        update_fields = []
        if not coach.is_active:
            coach.is_active = True
            update_fields.append("is_active")
        if team is not None and coach.team_id != team.id:
            coach.team = team
            update_fields.append("team")
        _sync_photo_url(coach, coach_data, update_fields)
        if update_fields:
            coach.save(update_fields=update_fields + ["updated_at"])
        return coach

    first_name, last_name, name_source = _resolve_cyrillic_name(coach_data, "тренера")
    coach = Coach.objects.create(
        sportmonks_id=str(sm_id), first_name=first_name, last_name=last_name,
        is_active=True, team=team, name_source=name_source, photo_url=sportmonks_photo_url(coach_data),
    )
    logger.info("Sportmonks: создан тренер %s (sportmonks_id=%s)", coach.full_name, sm_id)
    return coach


# Меньше игроков с действующим контрактом — состав у поставщика считаем устаревшим и не применяем.
SQUAD_MIN_PLAYERS = 11


def sync_current_squad(client, team: Team, today=None) -> tuple[bool, int, str]:
    """Текущий состав клуба из Sportmonks → player.team и squad_confirmed_at.
    Применяем только свежий состав (SQUAD_MIN_PLAYERS с действующим контрактом), иначе клуб определяется по последнему матчу.
    Возвращает (применён, сколько игроков подтверждено, причина)."""
    today = today or timezone.localdate()
    rows = client.get_team_squad(int(team.sportmonks_id), include="player")

    def active(row) -> bool:
        start, end = (row.get("start") or "")[:10], (row.get("end") or "")[:10]
        return (not start or start <= today.isoformat()) and (not end or end >= today.isoformat())

    current = [r for r in rows if r.get("player") and active(r)]
    if len(current) < SQUAD_MIN_PLAYERS:
        return False, 0, f"у поставщика {len(current)} игроков с действующим контрактом — состав устарел"
    now = timezone.now()
    confirmed = 0
    with transaction.atomic():
        for row in current:
            # Состав свежий: игрок в этом клубе сейчас, даже если последний матч сыграл за прошлый.
            player = get_or_create_player(row["player"], team=None)
            if player is None:
                continue
            fields = {"team": team, "squad_confirmed_at": now, "is_active": True}
            if row.get("jersey_number"):
                fields["number"] = row["jersey_number"]
            Player.objects.filter(pk=player.pk).update(**fields)
            confirmed += 1
        Team.objects.filter(pk=team.pk).update(squad_synced_at=now)
    return True, confirmed, ""


# Поля полного объекта игрока: по ним имя надёжно (словарь исправлений, проверенные правки ФИО).
FULL_NAME_KEYS = ("firstname", "lastname", "display_name", "common_name")


def get_or_create_player(
    player_data: Optional[Dict], team: Optional[Team] = None, number: Optional[int] = None,
    match_start_time=None,
) -> Optional[Player]:
    """player_data — lineups[].player.

    team/number/position обновляем только если матч не старее уже известного
    last_match_at: при бэкафилле порядок сезонов не хронологический, и старый
    матч откатывал бы игрока в прошлый клуб. Имя обновляем всегда.
    """
    if not player_data:
        return None
    sm_id = player_data.get("id")
    if sm_id is None:
        return None

    first_name, last_name, name_source = _resolve_cyrillic_name(player_data, "игрока")
    existing = player_by_sportmonks_id(sm_id)

    is_more_recent = (
        match_start_time is None
        or existing is None
        or existing.last_match_at is None
        or match_start_time >= existing.last_match_at
    )
    # Матч до подтверждения в текущем составе не возвращает игрока в прошлый клуб.
    if (is_more_recent and existing is not None and match_start_time is not None
            and existing.squad_confirmed_at and match_start_time < existing.squad_confirmed_at):
        is_more_recent = False

    defaults = {
        "first_name": first_name,
        "last_name": last_name,
        "name_source": name_source,
        "is_active": True,
    }
    # Только короткое имя (из события или состава без объекта игрока) — известное имя не портим.
    if existing is not None and not any(player_data.get(k) for k in FULL_NAME_KEYS):
        for key in ("first_name", "last_name", "name_source"):
            defaults.pop(key)
    if sportmonks_photo_url(player_data):
        defaults["photo_url"] = sportmonks_photo_url(player_data)
    if is_more_recent and "position_id" in player_data:
        defaults["position"] = POSITION_ID_MAP.get(player_data.get("position_id"), "")
    if is_more_recent:
        if team is not None:
            defaults["team"] = team
        if number is not None:
            defaults["number"] = number
        if match_start_time is not None:
            defaults["last_match_at"] = match_start_time

    if existing is None:
        # Игрок раньше приходил без id (только имя) — привязываем ту запись, а не плодим дубль.
        placeholder = _find_unlinked_player(first_name, last_name, team)
        if placeholder is not None:
            Player.objects.filter(pk=placeholder.pk).update(sportmonks_id=str(sm_id))
            logger.info("Sportmonks: игрок %s без id получил sportmonks_id=%s", placeholder.full_name, sm_id)
    if existing is not None:
        # По основному или дополнительному id (алиас после слияния) — обновляем нашу запись, id не трогаем.
        Player.objects.filter(pk=existing.pk).update(**defaults, updated_at=timezone.now())
        player, created = Player.objects.get(pk=existing.pk), False
    else:
        player, created = Player.objects.update_or_create(sportmonks_id=str(sm_id), defaults=defaults)
    if created:
        logger.info("Sportmonks: создан игрок %s (sportmonks_id=%s)", player.full_name, sm_id)
        _flag_potential_duplicate_player(player, team)
    elif not is_more_recent:
        logger.debug(
            "Sportmonks: игрок %s (sportmonks_id=%s) — фикстура %s старше уже известного "
            "last_match_at, team/number/position НЕ тронуты (защита от отката бэкафиллом не "
            "по хронологии)",
            player.full_name, sm_id, match_start_time,
        )
    return player


def _find_unlinked_player(first_name: str, last_name: str, team: Optional[Team]) -> Optional[Player]:
    """Игрок команды без sportmonks_id с тем же ФИО (сохранён по имени, когда поставщик не дал id)."""
    if team is None or not last_name:
        return None
    key = (normalize_kz(first_name.strip()), normalize_kz(last_name.strip()))
    for candidate in Player.objects.filter(team=team, sportmonks_id__isnull=True).only("id", "first_name", "last_name"):
        if (normalize_kz(candidate.first_name.strip()), normalize_kz(candidate.last_name.strip())) == key:
            return candidate
    return None


def _find_team_player_by_name(first_name: str, last_name: str, team: Team) -> Optional[Player]:
    """Известный игрок команды с тем же именем и фамилией (фамилия может быть короче: «Эвертон Мораес»
    = «Эвертон Маседо Мораес»). Только если кандидат один — иначе лучше новая запись, чем чужой человек."""
    first, last = normalize_kz(first_name.strip()), normalize_kz(last_name.strip())
    if not first or not last:
        return None
    found = [p for p in Player.objects.filter(team=team).only("id", "first_name", "last_name")
             if normalize_kz(p.first_name.strip()) == first
             and normalize_kz(p.last_name.strip()).split()[-1:] == last.split()[-1:]]
    return found[0] if len(found) == 1 else None


def _find_unique_player_by_full_name(first_name: str, last_name: str) -> Optional[Player]:
    """Точное ФИО во всей базе, если такой игрок один: в старом матче он мог играть за прошлый клуб."""
    key = (normalize_kz(first_name.strip()), normalize_kz(last_name.strip()))
    if not all(key):
        return None
    found = [p for p in Player.objects.filter(last_name__iexact=last_name.strip()).only("id", "first_name", "last_name")
             if (normalize_kz(p.first_name.strip()), normalize_kz(p.last_name.strip())) == key]
    return found[0] if len(found) == 1 else None


def get_or_create_unlinked_player(name: str, team: Optional[Team], number: Optional[int] = None,
                                  match_start_time=None) -> Optional[Player]:
    """Строка состава или события без id игрока, но с именем: без неё в старте 10 человек, а гол без автора."""
    name = (name or "").strip()
    if not name or team is None:
        return None
    first_name, last_name, name_source = _resolve_cyrillic_name({"name": name, "display_name": name}, "игрока")
    player = (_find_unlinked_player(first_name, last_name, team) or _find_team_player_by_name(first_name, last_name, team)
              or _find_unique_player_by_full_name(first_name, last_name))
    if player is None:
        player = Player.objects.create(first_name=first_name, last_name=last_name, name_source=name_source,
                                       team=team, number=number, is_active=True, last_match_at=match_start_time)
        logger.warning("Sportmonks: игрок «%s» пришёл без id — сохранён по имени в «%s»", name, team.name)
    return player


def _flag_potential_duplicate_player(new_player: Player, team: Optional[Team]) -> None:
    """Ставит флаг PotentialDuplicatePlayer, если в команде уже есть игрок с тем же ФИО,
    но другим sportmonks_id. Автоматически не сливаем — разбор вручную в админке.
    """
    if team is None:
        return
    norm_first = normalize_kz(new_player.first_name.strip())
    norm_last = normalize_kz(new_player.last_name.strip())
    if not norm_first or not norm_last:
        return

    existing_match = None
    for candidate in Player.objects.filter(team=team).exclude(id=new_player.id).only("id", "first_name", "last_name", "sportmonks_id"):
        if (
            normalize_kz(candidate.first_name.strip()) == norm_first
            and normalize_kz(candidate.last_name.strip()) == norm_last
        ):
            existing_match = candidate
            break
    if existing_match is None:
        return

    PotentialDuplicatePlayer.objects.get_or_create(
        existing_player=existing_match, new_player=new_player,
        defaults={"team_label": team.name},
    )
    logger.warning(
        "Sportmonks: возможный дубль игрока — %s (id=%s, sm_id=%s) и %s (id=%s, sm_id=%s) в команде «%s» "
        "— флаг поставлен в PotentialDuplicatePlayer, слияние только вручную",
        existing_match.full_name, existing_match.id, existing_match.sportmonks_id,
        new_player.full_name, new_player.id, new_player.sportmonks_id, team.name,
    )


# ============================================================================
# Вспомогательные функции матча
# ============================================================================

def _parse_starting_at(value: Optional[str]):
    """starting_at от Sportmonks — строка в UTC."""
    if not value:
        return None
    try:
        naive = datetime.fromisoformat(value.replace(" ", "T"))
    except ValueError:
        logger.warning("Sportmonks: не распарсил starting_at=%r", value)
        return None
    if timezone.is_aware(naive):
        return naive
    return timezone.make_aware(naive, dt_timezone.utc)


def _extract_tour(round_data: Optional[Dict]) -> Optional[int]:
    """round.name — номер тура строкой; регекс на случай «Round 21»."""
    if not round_data:
        return None
    name = round_data.get("name")
    if name is None:
        return None
    text = str(name).strip()
    try:
        return int(text)
    except ValueError:
        match = re.search(r"(\d+)", text)
        return int(match.group(1)) if match else None


def _build_referee_crew(referees_data: List[Dict]) -> Tuple[Optional[Referee], Optional[Dict]]:
    main_referee = None
    crew: Dict[str, str] = {}
    for entry in referees_data or []:
        ref_obj = entry.get("referee")
        if not ref_obj:
            continue
        type_id = entry.get("type_id")
        if type_id == REFEREE_TYPE_MAIN:
            main_referee = get_or_create_referee(ref_obj)
        else:
            label = REFEREE_CREW_LABELS.get(type_id)
            if label:
                crew[label] = ref_obj.get("name")
            else:
                logger.info(
                    "Sportmonks: неизвестный type_id=%s в бригаде судей, пропущен", type_id
                )
    return main_referee, (crew or None)


def _compute_field_positions(starters: List[Dict]) -> Dict[int, str]:
    """Зоны по строке формации — см. _ZONE_BY_ROW_SIZE."""
    rows: Dict[str, List[Dict]] = {}
    for entry in starters:
        ff = entry.get("formation_field")
        if not ff or ":" not in ff:
            continue
        row, _, _col = ff.partition(":")
        rows.setdefault(row, []).append(entry)

    result: Dict[int, str] = {}
    for row_entries in rows.values():
        try:
            row_entries.sort(key=lambda e: int(e["formation_field"].split(":")[1]))
        except (KeyError, ValueError, IndexError):
            continue
        zones = _ZONE_BY_ROW_SIZE.get(len(row_entries))
        if not zones:
            # Больше 5 игроков в строке — зону не угадываем.
            continue
        for entry, zone in zip(row_entries, zones):
            result[entry["id"]] = zone
    return result


def _stat_value(entry: Dict):
    value = (entry.get("data") or {}).get("value")
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    return None


# ============================================================================
# Матч
# ============================================================================

def _record_discrepancies(match: Match, before: Match, new_values: Dict) -> None:
    """Пишет ParserDiscrepancy, если у завершённого матча источник поменял счёт или статус."""
    label = f"{match.home_team} — {match.away_team}"[:200]
    for field in ("home_score", "away_score", "status"):
        old, new = getattr(before, field), new_values.get(field)
        if old is None or new is None or old == new:
            continue
        ParserDiscrepancy.objects.create(
            match=match, match_label=label, field_name=field,
            old_value=str(old), new_value=str(new),
        )
        logger.warning("Sportmonks: расхождение у завершённого матча %s: %s %s -> %s", match.id, field, old, new)


def is_placeholder_kickoff(dt) -> bool:
    """Полночь UTC — у поставщика это «время не объявлено» (05:00 по Алматы), а не настоящее время."""
    if dt is None:
        return False
    utc = dt.astimezone(dt_timezone.utc)
    return utc.hour == 0 and utc.minute == 0


# Матчи КПЛ не начинаются ночью: «перенос» на такое время — ошибка данных, подписчиков не тревожим.
PLAUSIBLE_KICKOFF_HOURS = range(9, 24)

# Сдвиг времени начала, о котором сообщаем подписчикам, и горизонт «ближайших» матчей.
KICKOFF_SHIFT_NOTIFY_MIN = timedelta(minutes=60)
KICKOFF_SHIFT_NOTIFY_HORIZON = timedelta(days=14)


def _detect_match_change(before: Match, match: Match) -> str | None:
    """postponed / cancelled / rescheduled — если это стоит сообщить подписчикам, иначе None."""
    now = timezone.now()
    if before.status == "scheduled" and match.status in ("postponed", "cancelled"):
        return match.status if before.start_time and before.start_time > now - timedelta(hours=3) else None
    if before.status in ("scheduled", "postponed") and match.status == "scheduled":
        if not before.start_time or not match.start_time or match.start_time <= now:
            return None
        if match.start_time - now > KICKOFF_SHIFT_NOTIFY_HORIZON and before.start_time - now > KICKOFF_SHIFT_NOTIFY_HORIZON:
            return None
        if before.status == "scheduled":
            old_local, new_local = timezone.localtime(before.start_time), timezone.localtime(match.start_time)
            # Время объявили или убрали в тот же день — это не перенос.
            if ((is_placeholder_kickoff(before.start_time) or is_placeholder_kickoff(match.start_time))
                    and old_local.date() == new_local.date()):
                return None
            if not is_placeholder_kickoff(match.start_time) and new_local.hour not in PLAUSIBLE_KICKOFF_HOURS:
                return None
        if before.status == "postponed" or abs(match.start_time - before.start_time) >= KICKOFF_SHIFT_NOTIFY_MIN:
            return "rescheduled"
    return None


@transaction.atomic
def import_match_core(fixture_data: Dict, league: League, season: Season) -> Match:
    """Создаёт/обновляет Match по sportmonks_id.

    Если league_id фикстуры не совпадает с нашей лигой — импорт прерывается
    (защита от записи чужой лиги под видом КПЛ).
    """
    sm_id = str(fixture_data["id"])

    fixture_league_id = fixture_data.get("league_id")
    if fixture_league_id is not None and str(fixture_league_id) != str(league.sportmonks_id):
        raise ValueError(
            f"Fixture {sm_id}: league_id={fixture_league_id} не совпадает с ожидаемой "
            f"лигой (sportmonks_id={league.sportmonks_id}) — импорт отклонён"
        )

    # Сезон — по самому матчу: календарь следующего сезона приходит, пока активен прошлый.
    fixture_season_id = fixture_data.get("season_id")
    if fixture_season_id is not None and str(fixture_season_id) != str(season.sportmonks_id):
        own_season = Season.objects.filter(sportmonks_id=str(fixture_season_id)).first()
        if own_season is None:
            raise ValueError(f"Fixture {sm_id}: сезон {fixture_season_id} ещё не заведён — импорт отложен до sync_season")
        season = own_season

    participants = fixture_data.get("participants") or []
    home_data = next((p for p in participants if (p.get("meta") or {}).get("location") == "home"), None)
    away_data = next((p for p in participants if (p.get("meta") or {}).get("location") == "away"), None)
    if not home_data or not away_data:
        raise ValueError(f"Fixture {sm_id}: нет home/away в participants (нужен include=participants)")

    home_team = get_or_create_team(home_data)
    away_team = get_or_create_team(away_data)

    state = fixture_data.get("state") or {}
    dev_name = state.get("developer_name") or "NS"
    status = STATE_MAP.get(dev_name)
    if status is None and dev_name != "AWAITING_UPDATES":
        logger.warning("Sportmonks: fixture %s — неизвестный state.developer_name=%r, статус не меняю", sm_id, dev_name)

    start_time = _parse_starting_at(fixture_data.get("starting_at"))

    home_score = away_score = None
    for score in fixture_data.get("scores") or []:
        if score.get("description") != "CURRENT":
            continue
        score_obj = score.get("score") or {}
        goals = score_obj.get("goals")
        participant = score_obj.get("participant")
        if participant == "home":
            home_score = goals
        elif participant == "away":
            away_score = goals

    tour = _extract_tour(fixture_data.get("round"))
    main_referee, referee_crew = _build_referee_crew(fixture_data.get("referees") or [])
    # Технический результат: составов и событий у такого матча не будет.
    decided_administratively = dev_name in ("AWARDED", "WO")

    existing = Match.objects.filter(sportmonks_id=sm_id).only(
        "id", "manual_override", "status", "start_time", "end_time", "voting_open_until",
        "home_score", "away_score",
    ).first()
    if status is None:
        status = existing.status if existing else "scheduled"

    defaults = {
        "league": league,
        "season": season,
        "home_team": home_team,
        "away_team": away_team,
        "home_score": home_score,
        "away_score": away_score,
        "referee": main_referee,
        "referee_crew": referee_crew,
        "tour": tour,
        "decided_administratively": decided_administratively,
    }

    # При manual_override статус и даты не трогаем — их правил staff.
    if existing and existing.manual_override:
        defaults["status"] = existing.status
        defaults["start_time"] = existing.start_time
        defaults["end_time"] = existing.end_time
        defaults["voting_open_until"] = existing.voting_open_until
    else:
        defaults["status"] = status
        defaults["start_time"] = start_time or (existing.start_time if existing else timezone.now())
        if status == "finished":
            defaults["end_time"] = defaults["start_time"] + timedelta(hours=2) if defaults["start_time"] else None
            defaults["voting_open_until"] = (
                defaults["start_time"] + timedelta(hours=48) if defaults["start_time"] else timezone.now() + timedelta(hours=48)
            )
            if decided_administratively:
                # Техрезультат: игры не было — окно голосования сразу закрыто (все проверки смотрят на него).
                defaults["voting_open_until"] = defaults["start_time"]
        elif existing:
            defaults["end_time"] = existing.end_time
            defaults["voting_open_until"] = existing.voting_open_until
        else:
            placeholder_start = defaults["start_time"] or timezone.now()
            defaults["voting_open_until"] = placeholder_start + timedelta(hours=48)

    match, created = Match.objects.update_or_create(sportmonks_id=sm_id, defaults=defaults)

    if existing and existing.status == "finished":
        _record_discrepancies(match, existing, defaults)

    change = _detect_match_change(existing, match) if existing and not existing.manual_override else None
    if change and not is_quiet():
        from notifications.tasks import notify_followers_match_changed
        old_start = existing.start_time.isoformat() if existing.start_time else None
        transaction.on_commit(
            lambda: notify_followers_match_changed.delay(str(match.id), change, old_start)
        )

    TeamSeason.objects.get_or_create(team=home_team, season=season)
    TeamSeason.objects.get_or_create(team=away_team, season=season)

    logger.info(
        "Sportmonks: матч %s %s: %s vs %s (%s)",
        match.id, "создан" if created else "обновлён", home_team, away_team, status,
    )
    return match


@transaction.atomic
def import_coaches(match: Match, coaches_data: List[Dict]) -> bool:
    if not coaches_data:
        return False
    home_sm_id = str(match.home_team.sportmonks_id or "")
    away_sm_id = str(match.away_team.sportmonks_id or "")

    # Тренер завершённого матча — зафиксированный факт, не перезаписываем.
    already_locked_home = match.status == "finished" and match.home_coach_id is not None
    already_locked_away = match.status == "finished" and match.away_coach_id is not None

    update_fields: List[str] = []
    for coach_data in coaches_data:
        team_sm_id = str((coach_data.get("meta") or {}).get("participant_id") or "")
        if team_sm_id == home_sm_id and not already_locked_home:
            coach = get_or_create_coach(coach_data, match.home_team)
            if coach:
                match.home_coach = coach
                update_fields.append("home_coach")
        elif team_sm_id == away_sm_id and not already_locked_away:
            coach = get_or_create_coach(coach_data, match.away_team)
            if coach:
                match.away_coach = coach
                update_fields.append("away_coach")

    if update_fields:
        match.save(update_fields=update_fields + ["updated_at"])
    return bool(update_fields)


# События, участник которых точно был на поле за свою команду (автогол не берём: участник там — пострадавшая сторона).
FIELD_EVENT_TYPES = {"GOAL", "PENALTY", "MISSED_PENALTY", "SUBSTITUTION", "YELLOWCARD", "REDCARD", "YELLOWREDCARD"}


def _lineup_twin(match_lineup, event_name):
    """Игрок заявки этой команды с той же фамилией и первым инициалом, что в имени из события («A. Petrov»)."""
    first, last, _src = _resolve_cyrillic_name({"name": event_name or "", "display_name": event_name or ""}, "игрока")
    words = normalize_kz(f"{first} {last}".replace(".", " ")).split()
    if len(words) < 2:
        return None
    initial, surname = words[0][:1], words[-1]
    found = [lp.player for lp in MatchLineupPlayer.objects.filter(lineup=match_lineup).select_related("player")
             if normalize_kz(lp.player.last_name).split()[-1:] == [surname]
             and normalize_kz(lp.player.first_name)[:1] == initial]
    return found[0] if len(found) == 1 else None


def _add_players_from_events(match: Match, match_lineup, team, team_sm_id: str, events_data, saved_players: set) -> None:
    """У поставщика бывают дыры в заявке: игрок вышел на замену или забил, а в составе его нет.
    Дописываем его запасным, который выходил, — иначе болельщики не смогут его оценить."""
    for evt in events_data or []:
        dev_name = ((evt.get("type") or {}).get("developer_name") or "").upper()
        if dev_name not in FIELD_EVENT_TYPES or str(evt.get("participant_id") or "") != team_sm_id:
            continue
        if not evt.get("player_id"):
            continue
        twin = None if player_by_sportmonks_id(evt["player_id"]) else _lineup_twin(match_lineup, evt.get("player_name"))
        if twin is not None:
            # Тот же человек под другим id поставщика — запоминаем id, в заявку не дописываем.
            PlayerSportmonksAlias.objects.get_or_create(sportmonks_id=str(evt["player_id"]), defaults={"player": twin})
            logger.info("Sportmonks: id %s («%s») — тот же игрок, что %s; записан алиас",
                        evt["player_id"], evt.get("player_name"), twin)
            continue
        player = get_or_create_player({"id": evt["player_id"], "name": evt.get("player_name")}, team=team,
                                      number=None, match_start_time=match.start_time)
        if not player or player.id in saved_players:
            continue
        saved_players.add(player.id)
        MatchLineupPlayer.objects.create(
            lineup=match_lineup, player=player, is_starting=False,
            minute_in=evt.get("minute") if dev_name == "SUBSTITUTION" else None,
        )
        logger.info("Sportmonks: %s нет в заявке матча %s, добавлен по событию %s", player, match.id, dev_name)


@transaction.atomic
def import_lineups(match: Match, lineups_data: List[Dict], formations_data: Optional[List[Dict]] = None,
                   events_data: Optional[List[Dict]] = None) -> bool:
    if not lineups_data:
        return False

    # Поставщик бывает непоследователен: в составе игрок без id, а в событиях — с id. Имя -> id из событий.
    id_by_name: Dict[str, int] = {}
    for evt in events_data or []:
        for name_key, id_key in (("player_name", "player_id"), ("related_player_name", "related_player_id")):
            if evt.get(name_key) and evt.get(id_key):
                id_by_name[evt[name_key].strip().lower()] = evt[id_key]

    home_sm_id = str(match.home_team.sportmonks_id or "")
    away_sm_id = str(match.away_team.sportmonks_id or "")

    formations_by_side: Dict[str, str] = {}
    for f in formations_data or []:
        loc = f.get("location")
        formation = f.get("formation")
        if loc and formation:
            formations_by_side[loc] = formation

    by_team: Dict[str, List[Dict]] = {}
    for entry in lineups_data:
        by_team.setdefault(str(entry.get("team_id") or ""), []).append(entry)

    MatchLineup.objects.filter(match=match).delete()

    any_saved = False
    saved_players: set = set()  # все игроки обеих заявок
    lineups_by_team: Dict[str, tuple] = {}
    for team_sm_id, entries in by_team.items():
        if team_sm_id == home_sm_id:
            side, team = "home", match.home_team
        elif team_sm_id == away_sm_id:
            side, team = "away", match.away_team
        else:
            logger.warning(
                "Sportmonks: lineup team_id=%s не совпал ни с home, ни с away матча %s",
                team_sm_id, match.id,
            )
            continue

        starters = [e for e in entries if e.get("type_id") == LINEUP_TYPE_STARTING]
        substitutes = [e for e in entries if e.get("type_id") == LINEUP_TYPE_BENCH]
        field_positions = _compute_field_positions(starters)

        match_lineup = MatchLineup.objects.create(
            match=match, team=team, side=side, formation=formations_by_side.get(side, ""),
        )

        def _save_entry(entry: Dict, is_starting: bool) -> None:
            if (entry.get("player") or {}).get("id") or entry.get("player_id"):
                player = get_or_create_player(
                    entry.get("player") or {"id": entry.get("player_id"), "name": entry.get("player_name")},
                    team=team, number=entry.get("jersey_number"), match_start_time=match.start_time,
                )
            elif (entry.get("player_name") or "").strip().lower() in id_by_name:
                player = get_or_create_player(
                    {"id": id_by_name[entry["player_name"].strip().lower()], "name": entry["player_name"]},
                    team=team, number=entry.get("jersey_number"), match_start_time=match.start_time,
                )
            else:
                player = get_or_create_unlinked_player(entry.get("player_name"), team,
                                                       entry.get("jersey_number"), match.start_time)
            if not player:
                return
            saved_players.add(player.id)
            # Детальная позиция в приоритете, грубая — fallback.
            # Ключ читаем в обоих регистрах на всякий случай.
            detailed = entry.get("detailedPosition") or entry.get("detailedposition") or {}
            detailed_id = detailed.get("id") if isinstance(detailed, dict) else None
            position_code = (
                DETAILED_POSITION_ID_MAP.get(detailed_id)
                or POSITION_ID_MAP.get(entry.get("position_id"), "")
            )
            if entry.get("position_id") and not detailed_id:
                # Логируем, если detailedPosition не пришёл.
                logger.debug(
                    "Sportmonks: lineup entry id=%s (match %s) — нет detailedPosition в "
                    "ответе, используется грубый POSITION_ID_MAP (position_id=%s)",
                    entry.get("id"), match.id, entry.get("position_id"),
                )
            MatchLineupPlayer.objects.create(
                lineup=match_lineup,
                player=player,
                is_starting=is_starting,
                position=position_code,
                field_position=field_positions.get(entry.get("id"), ""),
                shirt_number=entry.get("jersey_number"),
                minute_in=0 if is_starting else None,
                minute_out=None,
            )

        for entry in starters:
            _save_entry(entry, True)
        for entry in substitutes:
            _save_entry(entry, False)
        lineups_by_team[team_sm_id] = (match_lineup, team)

        any_saved = True

    # Добор по событиям — после обеих заявок: у поставщика бывает перепутана команда события,
    # и игрок соперника не должен попасть в чужую заявку.
    for team_sm_id, (match_lineup, team) in lineups_by_team.items():
        _add_players_from_events(match, match_lineup, team, team_sm_id, events_data, saved_players)

    if any_saved and not match.has_lineup:
        match.has_lineup = True
        match.save(update_fields=["has_lineup", "updated_at"])
    return any_saved


def _dedupe_by_id(events_data: List[Dict]) -> List[Dict]:
    """Одно событие поставщика может прийти дважды (гол и его правка в автогол) — берём последнюю версию."""
    last_index = {str(e.get("id")): i for i, e in enumerate(events_data) if e.get("id")}
    return [e for i, e in enumerate(events_data) if not e.get("id") or last_index[str(e.get("id"))] == i]


@transaction.atomic
def import_events(match: Match, events_data: List[Dict]) -> bool:
    """Импорт событий матча. Возвращает новые события (и переклассифицированные
    в push-достойный тип) — по ним вызывающий код шлёт пуши.

    Сопоставление в первую очередь по sportmonks_id события: так VAR-замена
    жёлтой на красную обновляет ту же запись, а не создаёт дубль.
    (minute, event_type, team_side) — fallback для событий без id.
    """
    if not events_data:
        return []

    # Локальный импорт — избегаем циклического импорта.
    from notifications.tasks import PUSH_WORTHY_EVENT_TYPES

    home_sm_id = str(match.home_team.sportmonks_id or "")
    away_sm_id = str(match.away_team.sportmonks_id or "")

    # Сначала по sportmonks_id, эвристика — fallback для строк без id.
    existing_by_sm_id: Dict[str, MatchEvent] = {}
    existing_pool: Dict[tuple, List[MatchEvent]] = {}
    for ev in match.events.all():
        if ev.sportmonks_id:
            existing_by_sm_id[ev.sportmonks_id] = ev
        else:
            key = (ev.minute, ev.event_type, ev.team_side)
            existing_pool.setdefault(key, []).append(ev)

    # Сторона игрока по заявке: у поставщика бывает перепутан participant_id у карточек.
    lineup_side = dict(MatchLineupPlayer.objects.filter(lineup__match=match).values_list("player_id", "lineup__side"))

    created_count = updated_count = reclassified_count = skipped_count = 0
    # Возвращаем и события, сменившие тип на push-достойный (жёлтая → красная).
    push_candidate_events: List[MatchEvent] = []

    for evt in _dedupe_by_id(events_data):
        dev_name = (evt.get("type") or {}).get("developer_name") or ""
        event_type = EVENT_DEV_NAME_MAP.get(dev_name)
        if event_type is None:
            logger.info(
                "Sportmonks: неизвестный тип события developer_name=%r (матч %s), пропущено",
                dev_name, match.id,
            )
            skipped_count += 1
            continue

        minute = evt.get("minute")
        if minute is None:
            continue

        participant_id = str(evt.get("participant_id") or "")
        if participant_id == home_sm_id:
            team_side = "home"
        elif participant_id == away_sm_id:
            team_side = "away"
        else:
            logger.warning(
                "Sportmonks: событие матча %s с неизвестным participant_id=%s",
                match.id, participant_id,
            )
            continue

        player = assist_player = player_out = None
        score_after = evt.get("result") or ""
        added_time = evt.get("extra_minute") or 0

        player_sm_id = evt.get("player_id")
        related_sm_id = evt.get("related_player_id")

        if event_type == "substitution":
            # player_id — вошёл, related_player_id — ушёл.
            if player_sm_id:
                player = player_by_sportmonks_id(player_sm_id)
            if related_sm_id:
                player_out = player_by_sportmonks_id(related_sm_id)
            if player:
                MatchLineupPlayer.objects.filter(lineup__match=match, player=player).update(
                    minute_in=minute, is_starting=False
                )
            if player_out:
                # Вошедший на замену наследует зону ушедшего игрока (API её не отдаёт),
                # если его зона ещё не известна.
                outgoing_row = MatchLineupPlayer.objects.filter(
                    lineup__match=match, player=player_out
                ).first()
                if outgoing_row:
                    outgoing_row.minute_out = minute
                    outgoing_row.save(update_fields=["minute_out"])
                    if player and outgoing_row.field_position:
                        MatchLineupPlayer.objects.filter(
                            lineup__match=match, player=player, field_position="",
                        ).update(field_position=outgoing_row.field_position)
        else:
            if player_sm_id:
                player = player_by_sportmonks_id(player_sm_id)
            elif evt.get("player_name") and event_type != "own_goal":
                side_team = match.home_team if team_side == "home" else match.away_team
                first, last, _src = _resolve_cyrillic_name({"name": evt["player_name"]}, "игрока")
                player = _find_unlinked_player(first, last, side_team)
            if event_type in ("goal", "penalty", "own_goal") and related_sm_id:
                assist_player = player_by_sportmonks_id(related_sm_id)
            # Автогол — игрок соперника, там сторона = кому засчитан; остальным сторона по заявке.
            if player and event_type != "own_goal" and lineup_side.get(player.id) not in (None, team_side):
                logger.warning("Sportmonks: событие %s матча %s — сторона %s по поставщику, %s по заявке; беру заявку",
                               evt.get("id"), match.id, team_side, lineup_side[player.id])
                team_side = lineup_side[player.id]

        evt_sm_id = str(evt.get("id") or "") or None

        matched_existing = existing_by_sm_id.pop(evt_sm_id, None) if evt_sm_id else None
        if matched_existing is None:
            matched_key = (minute, event_type, team_side)
            bucket = existing_pool.get(matched_key)
            matched_existing = bucket.pop(0) if bucket else None

        if matched_existing is not None:
            # Sportmonks иногда повторно присылает то же событие «пустым» (без игрока).
            # Не даём ему затереть уже сохранённые данные.
            incoming_has_player_info = bool(player_sm_id) or bool(evt.get("player_name"))
            existing_has_player_info = bool(matched_existing.player_id) or bool(
                (matched_existing.extra_data or {}).get("player_name")
            )
            if not incoming_has_player_info and existing_has_player_info:
                logger.info(
                    "Sportmonks: событие %s (sportmonks_id=%s) матча %s — новый приход без данных "
                    "игрока поверх уже сохранённых данных, игнорирую (защита от регрессии)",
                    matched_existing.id, evt_sm_id, match.id,
                )
                skipped_count += 1
                continue

            previous_type = matched_existing.event_type
            matched_existing.player = player
            matched_existing.minute = minute
            matched_existing.added_time = added_time
            matched_existing.event_type = event_type
            matched_existing.team_side = team_side
            matched_existing.assist_player = assist_player
            matched_existing.score_after = score_after
            matched_existing.player_out = player_out
            matched_existing.extra_data = evt
            matched_existing.sportmonks_id = evt_sm_id
            matched_existing.save()
            updated_count += 1
            if previous_type != event_type:
                reclassified_count += 1
                logger.info(
                    "Sportmonks: событие %s матча %s переклассифицировано %r → %r "
                    "(коррекция/VAR, минута %s)",
                    matched_existing.id, match.id, previous_type, event_type, minute,
                )
                if event_type in PUSH_WORTHY_EVENT_TYPES:
                    push_candidate_events.append(matched_existing)
        else:
            fields = dict(player=player, minute=minute, added_time=added_time, event_type=event_type,
                          team_side=team_side, assist_player=assist_player, score_after=score_after,
                          player_out=player_out, extra_data=evt)
            if evt_sm_id:
                # update_or_create по уникальному (match, sportmonks_id): параллельный импорт не создаст дубль.
                new_event, created = MatchEvent.objects.update_or_create(
                    match=match, sportmonks_id=evt_sm_id, defaults=fields)
            else:
                new_event, created = MatchEvent.objects.create(match=match, **fields), True
            if created:
                created_count += 1
                push_candidate_events.append(new_event)
            else:
                updated_count += 1

    removed_count = _drop_withdrawn_events(match, events_data)
    logger.info(
        "Sportmonks: события матча %s — %s новых, %s обновлено (из них %s переклассифицировано), "
        "%s пропущено (неизв. тип), %s снято поставщиком",
        match.id, created_count, updated_count, reclassified_count, skipped_count, removed_count,
    )
    return push_candidate_events


# Больше стольких пропавших событий разом — похоже на обрезанный ответ, а не на отмену: не удаляем.
WITHDRAWN_MAX_ABS = 3
WITHDRAWN_MAX_SHARE = 0.2


def _drop_withdrawn_events(match: Match, events_data: List[Dict]) -> int:
    """Событие, которого больше нет в полной ленте поставщика (отменённый гол), удаляем — иначе счёт по событиям врёт."""
    incoming = {str(e["id"]) for e in events_data if e.get("id")}
    if not incoming:
        return 0
    stored = match.events.exclude(sportmonks_id__isnull=True)
    stale = list(stored.exclude(sportmonks_id__in=incoming))
    if not stale:
        return 0
    if len(stale) > max(WITHDRAWN_MAX_ABS, WITHDRAWN_MAX_SHARE * stored.count()):
        logger.warning("Sportmonks: у матча %s пропало %s событий разом — не удаляю, похоже на неполный ответ",
                       match.id, len(stale))
        return 0
    for event in stale:
        logger.info("Sportmonks: событие %s (%s %s', sportmonks_id=%s) матча %s снято поставщиком — удаляю",
                    event.id, event.event_type, event.minute, event.sportmonks_id, match.id)
    MatchEvent.objects.filter(id__in=[e.id for e in stale]).delete()
    return len(stale)


@transaction.atomic
def import_statistics(match: Match, fixture_statistics: List[Dict]) -> bool:
    """Командная статистика (fixture.statistics)."""
    if not fixture_statistics:
        return False
    home_sm_id = str(match.home_team.sportmonks_id or "")
    away_sm_id = str(match.away_team.sportmonks_id or "")

    by_team: Dict[str, Dict] = {}
    # В raw сохраняем все пришедшие типы, не только разобранные.
    raw_by_team: Dict[str, Dict] = {}
    for entry in fixture_statistics:
        dev_name = (entry.get("type") or {}).get("developer_name")
        participant_id = str(entry.get("participant_id") or "")
        if dev_name:
            raw_by_team.setdefault(participant_id, {})[dev_name] = (entry.get("data") or {}).get("value")
        field = TEAM_STAT_DEV_NAME_MAP.get(dev_name)
        if not field:
            continue
        by_team.setdefault(participant_id, {})[field] = _stat_value(entry)

    saved = 0
    for participant_id, fields in by_team.items():
        if participant_id == home_sm_id:
            team = match.home_team
        elif participant_id == away_sm_id:
            team = match.away_team
        else:
            continue
        defaults = dict(fields)
        defaults["raw"] = raw_by_team.get(participant_id, {})
        MatchTeamStatistics.objects.update_or_create(match=match, team=team, defaults=defaults)
        saved += 1

    logger.info("Sportmonks: командная статистика матча %s — %s команд(ы)", match.id, saved)
    return bool(saved)


@transaction.atomic
def import_player_statistics(match: Match, lineups_data: List[Dict]) -> bool:
    if not lineups_data:
        return False

    home_sm_id = str(match.home_team.sportmonks_id or "")
    away_sm_id = str(match.away_team.sportmonks_id or "")

    saved = 0
    for entry in lineups_data:
        details = entry.get("details") or []
        if not details:
            continue
        player_sm_id = entry.get("player_id")
        if player_sm_id is None:
            continue
        player = player_by_sportmonks_id(player_sm_id)
        if player is None:
            continue

        team_sm_id = str(entry.get("team_id") or "")
        if team_sm_id == home_sm_id:
            team = match.home_team
        elif team_sm_id == away_sm_id:
            team = match.away_team
        else:
            continue

        fields: Dict = {}
        # В raw — все типы из details (отборы, перехваты, оценка и т.д.).
        raw: Dict = {}
        for d in details:
            dev_name = (d.get("type") or {}).get("developer_name")
            if dev_name:
                raw[dev_name] = (d.get("data") or {}).get("value")
            field = PLAYER_STAT_DEV_NAME_MAP.get(dev_name)
            if field:
                fields[field] = _stat_value(d)

        if not fields and not raw:
            continue
        defaults = dict(fields)
        defaults["team"] = team
        defaults["raw"] = raw
        MatchPlayerStatistics.objects.update_or_create(match=match, player=player, defaults=defaults)
        saved += 1

    logger.info("Sportmonks: игровая статистика матча %s — %s игрок(ов)", match.id, saved)
    return bool(saved)


# --- недоступность игроков ---
# Поля читаем защитно в нескольких вариантах. Неизвестная категория
# сохраняется как 'other' с warning в логе.
SIDELINED_CATEGORY_MAP = {
    "injury": "injury",
    "cardiovascular": "injury",
    "questionable": "injury",
    "illness": "injury",
    "suspended": "suspended",
    "suspension": "suspended",
    "red-card-suspension": "suspended",
    "yellow-card-suspension": "suspended",
    "national-duty-suspension": "suspended",
}


def _parse_date_only(raw) -> Optional[object]:
    if not raw:
        return None
    try:
        return datetime.strptime(str(raw)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


# Машинный перевод типов у поставщика -> нормальный русский; «неизвестная травма» = просто «Травма».
SIDELINED_REASON_FIXES = {
    "аннулирование за жёлтую карточку": "Перебор жёлтых карточек",
    "аннулирование за желтую карточку": "Перебор жёлтых карточек",
    "yellow card suspension": "Перебор жёлтых карточек",
    "дисквалификация по красной карточке": "Красная карточка",
    "red card suspension": "Красная карточка",
    "неизвестная травма": "",
    "unknown injury": "",
}


def sidelined_reason(name) -> str:
    name = (name or "").strip()
    fixed = SIDELINED_REASON_FIXES.get(name.lower())
    return fixed if fixed is not None else name[:120]


@transaction.atomic
def import_sidelined(team: Team, sidelined_data: List[Dict]) -> int:
    """Травмы/дисквалификации игроков одной команды.

    Это текущий статус: записи, пропавшие из ответа, удаляются.
    Игроков, которых ещё нет в базе, пропускаем.
    """
    seen_sm_ids = set()
    saved = 0
    for entry in sidelined_data or []:
        sm_id = entry.get("id")
        if sm_id is None:
            continue
        sideline = entry.get("sideline") or {}
        player_data = entry.get("player") or {}
        player_sm_id = entry.get("player_id") or player_data.get("id")
        if player_sm_id is None:
            continue

        player = player_by_sportmonks_id(player_sm_id)
        if player is None:
            logger.info(
                "Sportmonks: sidelined id=%s -> player sportmonks_id=%s не найден в базе, пропуск",
                sm_id, player_sm_id,
            )
            continue

        if entry.get("completed") is True:
            continue  # травма прошла, дисквалификация отбыта

        category_raw = str(entry.get("category") or sideline.get("category") or "").lower()
        category = SIDELINED_CATEGORY_MAP.get(category_raw, "other")
        if category == "other" and category_raw:
            logger.info(
                "Sportmonks: sidelined id=%s — неизвестная category=%r, не найдена в "
                "SIDELINED_CATEGORY_MAP (parsers/sportmonks/importers.py), считаю 'other'",
                sm_id, category_raw,
            )

        start_date = _parse_date_only(entry.get("start_date") or sideline.get("start_date"))
        end_date = _parse_date_only(entry.get("end_date") or sideline.get("end_date"))

        PlayerSidelined.objects.update_or_create(
            sportmonks_id=str(sm_id),
            defaults={
                "player": player,
                "category": category,
                "start_date": start_date,
                "end_date": end_date,
                "reason": sidelined_reason((entry.get("type") or {}).get("name")),
                "games_missed": entry.get("games_missed"),
            },
        )
        seen_sm_ids.add(str(sm_id))
        saved += 1

    stale = PlayerSidelined.objects.filter(
        player__team=team, sportmonks_id__isnull=False,
    ).exclude(sportmonks_id__in=seen_sm_ids)
    stale_count = stale.count()
    if stale_count:
        stale.delete()

    logger.info(
        "Sportmonks: недоступность игроков %s — активных записей %s%s",
        team, saved, f", устаревших удалено {stale_count}" if stale_count else "",
    )
    return saved


def import_full_fixture(fixture_data: Dict, league: League, season: Season) -> Match:
    """Тяжёлый импорт одного матча (ответ get_fixture с HEAVY_FIXTURE_INCLUDE).

    Состояние матча читаем ДО импорта, чтобы поймать переходы и отправить
    уведомления: старт матча, составы доступны, матч завершён, новые события.
    Все задачи ставятся через transaction.on_commit.
    """
    sm_id = str(fixture_data.get("id"))
    existing_before = Match.objects.filter(sportmonks_id=sm_id).only("id", "status", "has_lineup").first()
    was_finished_before = existing_before is not None and existing_before.status == "finished"
    was_scheduled_before = existing_before is not None and existing_before.status == "scheduled"
    had_lineup_before = existing_before is not None and existing_before.has_lineup

    match = import_match_core(fixture_data, league=league, season=season)
    import_coaches(match, fixture_data.get("coaches") or [])
    import_lineups(match, fixture_data.get("lineups") or [], fixture_data.get("formations") or [],
                   events_data=fixture_data.get("events") or [])
    # Новые события и события, переклассифицированные в push-достойный тип.
    push_worthy_events = import_events(match, fixture_data.get("events") or [])
    import_statistics(match, fixture_data.get("statistics") or [])
    import_player_statistics(match, fixture_data.get("lineups") or [])

    # Приглашение оценить — только пока голосование открыто (бэкафилл старых матчей не спамит).
    voting_open = bool(match.voting_open_until and match.voting_open_until > timezone.now())
    # Тихий режим (переимпорт из архива, проигрывание) — без рассылок.
    if is_quiet():
        return match
    if match.status == "finished" and not was_finished_before and voting_open:
        from notifications.tasks import notify_followers_match_activity
        transaction.on_commit(lambda: notify_followers_match_activity.delay(str(match.id)))

    # Пуш о старте — только на реальный переход scheduled → live в этом синке.
    if match.status == "live" and was_scheduled_before:
        from notifications.tasks import notify_followers_match_started
        transaction.on_commit(lambda: notify_followers_match_started.delay(str(match.id)))

    # Пуш о составах — только пока матч не завершён.
    if match.has_lineup and not had_lineup_before and match.status in ("scheduled", "live"):
        from notifications.tasks import notify_followers_lineups_available
        transaction.on_commit(lambda: notify_followers_lineups_available.delay(str(match.id)))

    # Live-пуши о событиях — только пока матч идёт. Досинк завершённого матча
    # (сервер лежал, бэкафилл) не должен присылать пачку старых голов.
    if push_worthy_events and match.status == "live":
        from notifications.tasks import PUSH_WORTHY_EVENT_TYPES, notify_followers_match_event
        for event in push_worthy_events:
            if event.event_type in PUSH_WORTHY_EVENT_TYPES:
                transaction.on_commit(
                    lambda match_id=str(match.id), event_id=str(event.id): (
                        notify_followers_match_event.delay(match_id, event_id)
                    )
                )

    return match
