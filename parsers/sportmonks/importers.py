# parsers/sportmonks/importers.py
"""
Импортёры Sportmonks -> Django-модели (фаза 3, docs/sportmonks-migration-plan.md).

База была очищена (manage.py flush) ДО начала миграции — поэтому, в отличие
от parsers/kff/importers.py, здесь НЕТ логики "сопоставить с уже существующей
записью по имени/фаззи-мэтчу": каждая сущность создаётся/обновляется через
обычный update_or_create по sportmonks_id по-настоящему с нуля. Это сильно
проще и безопаснее write-side: если запись уже существует — id стабилен и не
меняется от повторного импорта.

ВСЕ маппинги ниже (developer_name событий/статистики, type_id бригады судей,
type_id расстановки/скамейки, id позиций 24/25/26/27) — НЕ придуманы, а
проверены вживую 2026-09-08 прямым запросом к
GET /fixtures/{id}?include=state;round;venue;participants;formations;
lineups.type;lineups.player;lineups.details.type;events.type;events.player;
statistics.type;referees.type;referees.referee;coaches
на реальном сыгранном матче КПЛ (Тобол vs Кайсар, fixture 19681993,
2026-08-25) через тестовый ключ триала. Ключевой урок из этой проверки:
локализованные (`type.name`, RU) названия событий/статистики — машинный
перевод и НЕ пригодны для логики ("Йеллоукард." для жёлтой карточки,
"Штраф" для пенальти) — везде, где нужно ветвление по типу, используется
СТАБИЛЬНЫЙ `type.developer_name` (латиница, не зависит от locale). RU-имя
остаётся только в `raw`/`extra_data` для отображения и отладки.

Три места ПОМЕЧЕНЫ как эвристика/требуют проверки на большем числе матчей
перед тем, как полностью им доверять (см. комментарии на месте):
  1. `_compute_field_positions` — сторона в формации (L/C/R) выводится из
     `formation_field` ("row:col"), а не приходит готовой от API.
  2. `_parse_starting_at` — предположение, что Sportmonks отдаёт время в UTC.
  3. `EVENT_DEV_NAME_MAP` / `*_STAT_DEV_NAME_MAP` — на тестовом матче НЕ
     встретились автогол, незабитый пенальти, красная карточка, VAR — эти
     ветки написаны по документированным type_id/паттерну соседних типов,
     но не подтверждены живым примером. Если такое событие попадётся на
     реальном матче и тип не распознается — код не упадёт и не потеряет
     данные (raw/extra_data сохраняется всегда), но залогирует warning,
     который нужно будет разобрать и доопределить маппинг.
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
from players.models import Player, PlayerSidelined
from parsers.sportmonks.translit import is_likely_foreign, transliterate_name
from referees.models import Referee
from seasons.models import Season
from teams.models import Team, TeamSeason

logger = logging.getLogger(__name__)


# Полный include для "тяжёлого" запроса одного матча (см. client.py::get_fixture
# и план, фаза 4 — этот include дёргается ТОЛЬКО когда двухуровневая схема
# обнаружила реальное изменение состояния матча, никогда в цикле по всем матчам).
HEAVY_FIXTURE_INCLUDE = (
    # "venue" убран из include (2026-09-09) — Stadium удалён из проекта
    # целиком, нет смысла тянуть и парсить данные, которые никуда не пишем.
    # "lineups.detailedPosition" добавлен 2026-09-09 (вопрос пользователя
    # "у нас была проблема с позициями из-за парсинга КФФ, может тут в API
    # нет проблем?") — см. DETAILED_POSITION_ID_MAP ниже.
    #
    # ИСПРАВЛЕНО 2026-09-09 (второй проход, пользователь прислал скриншот —
    # на мини-схеме поля были только "D"/"M", то есть грубый POSITION_ID_MAP,
    # detailedPosition ни разу не сработал): первая версия использовала
    # "lineups.detailedposition" (всё строчными) по аналогии с "lineups.type"/
    # "lineups.player" — ОШИБКА, официальная документация Sportmonks
    # (docs.sportmonks.com/football/definitions/types/lineups-positions-and-
    # formations) называет этот include именно "detailedPosition" (camelCase,
    # заглавная P) — Sportmonks не игнорирует неизвестный include с ошибкой,
    # а просто молча не прикладывает связь, поэтому это не падало и не
    # логировалось, просто вечно скатывалось на fallback. См. также
    # defensive-lookup в import_lineups() ниже — на случай, если сам ключ
    # в JSON-ответе всё равно придёт в нижнем регистре.
    "state;round;participants;formations;scores;"
    "lineups.type;lineups.player;lineups.details.type;lineups.detailedPosition;"
    "events.type;events.player;"
    "statistics.type;referees.type;referees.referee;coaches"
)

# --- позиции игрока -----------------------------------------------------
# Core position_id у Sportmonks — проверено вживую (Salaydin/вратарь=24,
# Tulegenov/защитник=25, Agostinho/нападающий=27). Коды справа — те же,
# что уже используются в проекте (players/positions.py::POSITION_LABELS),
# подстановка напрямую совместима с существующей схемой отображения.
# Это ГРУБАЯ группа (только GK/защита/полузащита/атака) — используется как
# fallback, когда детальная позиция ниже недоступна.
POSITION_ID_MAP = {24: "GK", 25: "D", 26: "M", 27: "F"}

# Детальная позиция игрока В ЭТОМ КОНКРЕТНОМ МАТЧЕ — nested include
# "lineups.detailedposition" (id таблицы см. docs.sportmonks.com/football/
# definitions/types/lineups-positions-and-formations, раздел "Detailed
# Position Types"). НАЙДЕНО 2026-09-09 в ответ на вопрос пользователя про
# точность позиций (раньше у KFF-парсера позиции были "примерно" — см.
# докстринг players/positions.py): в отличие от POSITION_ID_MAP (всего 4
# грубые группы), это даёт РОВНО те же коды, что уже приняты в проекте
# (players/positions.py::POSITION_LABELS и SLOT_PROCESSING_ORDER пулы),
# то есть встаёт в существующую схему "Сборной сезона/тура" БЕЗ переделки
# season_squad/round_squad — они уже умеют принимать голый код "CB"/"RB"/
# "LW" и т.д. (см. SLOT_PROCESSING_ORDER, все пулы уже содержат эти коды).
# 163 (Secondary Striker) — отдельного кода в проекте нет, ближайший по
# смыслу — CF (центральный нападающий), маппим туда.
# ТРЕБУЕТ проверки вживую на первом матче с этим include (как и остальные
# id-таблицы Sportmonks в этом файле) — сверить с реальным составом на
# странице матча, прежде чем полагаться на бэкафилл всей истории.
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
    163: "CF",   # Secondary Striker — нет отдельного кода в проекте, ближе всего к CF
}

# --- статусы матча --------------------------------------------------------
# Матчим по СТАБИЛЬНОМУ state.developer_name, а не по числовому state_id —
# id теоретически может отличаться между окружениями/со временем, developer_name
# документирован Sportmonks как постоянный код. "FT" подтверждено вживую.
# Остальные — по документированному списку core-статусов Sportmonks; если
# реально встретится код, которого нет в этой таблице, код НЕ падает и НЕ
# тихо теряется — см. import_match_core, тот же паттерн диагностики, что и
# STATUS_MAP в parsers/kff/importers.py.
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
    "FT": "finished",
    "AET": "finished",
    "FT_PEN": "finished",
    "AWARDED": "finished",
    "WO": "finished",
    "ABANDONED": "finished",
    "POSTPONED": "postponed",
    "CANCELLED": "cancelled",
}

# --- бригада судей ---------------------------------------------------------
# type_id 6/7/8/9 и их developer_name (REFEREE/REFEREE_TWO/REFEREE_THREE/
# REFEREE_FOUR) — проверено вживую на реальном матче.
REFEREE_TYPE_MAIN = 6
REFEREE_CREW_LABELS = {7: "assistant_1", 8: "assistant_2", 9: "fourth_official"}

# --- расстановка/скамейка ---------------------------------------------------
LINEUP_TYPE_STARTING = 11  # developer_name LINEUP — подтверждено вживую
LINEUP_TYPE_BENCH = 12  # developer_name BENCH — подтверждено вживую

# --- определение "чистого" кириллического текста --------------------------
# НАЙДЕНО ВЖИВУЮ 2026-09-08 (пользователь заметил на сайте "Марин Беланчић",
# "Никола Антић" — сербские игроки): единого надёжного поля с переводом у
# Sportmonks НЕТ. У казахских игроков "name" переведён полностью, а
# firstname/lastname остаются латиницей (Salaydin: firstname="Nurimzhan").
# У балканских игроков — РОВНО НАОБОРОТ и вдобавок БИТО: "name" — смесь
# ("Марин" переведено, "Беланчић" — нет, да ещё с латинской диакритикой ć,
# не кириллической буквой), а firstname/lastname по отдельности оказались
# чистой кириллицей ("Никола"/"Антич"). Полагаться на одно фиксированное
# поле означает гарантированно ловить битые имена у части игроков — вместо
# этого пробуем несколько кандидатов по очереди и берём первый, который
# ПОЛНОСТЬЮ кириллический (ни одной латинской буквы/диакритики).
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


def _resolve_cyrillic_name(entity_data: Dict, entity_label: str) -> Tuple[str, str]:
    """Возвращает (first_name, last_name), предпочитая первого "чистого"
    кириллического кандидата среди firstname+lastname / name / display_name
    (в этом порядке — см. докстринг блока выше). Если ни один кандидат не
    оказался чистым, берёт лучший доступный текст и логирует warning —
    такую запись стоит поправить вручную в админке (см. list_latin_names.py)."""
    firstname = (entity_data.get("firstname") or "").strip()
    lastname = (entity_data.get("lastname") or "").strip()
    if _is_clean_cyrillic(firstname) and _is_clean_cyrillic(lastname):
        return firstname, lastname

    name = (entity_data.get("name") or "").strip()
    if _is_clean_cyrillic(name):
        return _split_name(name)

    display_name = (entity_data.get("display_name") or "").strip()
    # "Х. Фамилия" — сокращённая форма (инициал с точкой), не годится как
    # first_name даже если сама по себе кириллическая и чистая.
    if _is_clean_cyrillic(display_name) and not re.match(r"^[А-ЯЁ]\.\s", display_name):
        return _split_name(display_name)

    # Ни один кандидат не оказался чистой кириллицей — автоматически
    # транслитерируем (parsers/sportmonks/translit.py, см. чат с
    # пользователем 2026-09-08: "надо автоматизировать, чтобы всё
    # корректно было") вместо того, чтобы оставлять голую латиницу на
    # сайте. Предпочитаем firstname+lastname источником для транслитерации
    # (обычно самые "сырые"/надёжные поля), иначе name, иначе display_name.
    latin_source = (
        f"{firstname} {lastname}".strip() if (firstname or lastname) else (name or display_name)
    )
    if not latin_source:
        return "", ""

    # ИСПРАВЛЕНО (2026-09-09, баги найдены пользователем — "Владимир
    # Слиšковиć", "Йоãо Антóнио Ферреира Гонçалвес"): _SINGLE_CHAR_MAP в
    # translit.py не знает диакритику романских/южнославянских языков (š,
    # ć, ã, ó, ç...) и по докстрингу оставляет такие символы "как есть" —
    # is_likely_foreign() и раньше ловил именно такие имена, но раньше
    # использовался ТОЛЬКО для уровня логирования, транслитерация всё
    # равно запускалась. На явно не-славянском имени это давало не
    # "неидеальную кириллицу" (как задумывалось), а буквальную мешанину
    # кириллицы с необработанными латинскими символами внутри одного
    # слова — хуже чистой латиницы, а не лучше. Теперь для таких имён
    # транслитерация вообще не запускается — используется оригинальный
    # латинский текст с диакритикой как есть (João António Ferreira
    # Gonçalves, Vladimir Slišković): читаемо и правильно, точная
    # практическая транскрипция кириллицей по-прежнему возможна вручную
    # через админку в любой момент (как и для остальных имён).
    if is_likely_foreign(latin_source):
        logger.info(
            "Sportmonks: имя %s sportmonks_id=%s похоже на не-славянское — "
            "оставлено латиницей как есть %r вместо частичной/ломаной "
            "транслитерации (источник: name=%r, firstname=%r, lastname=%r, "
            "display_name=%r); практическую транскрипцию кириллицей можно "
            "выставить вручную в админке",
            entity_label, entity_data.get("id"), latin_source, name, firstname, lastname, display_name,
        )
        return _split_name(latin_source)

    transliterated = transliterate_name(latin_source)
    logger.info(
        "Sportmonks: имя %s sportmonks_id=%s автоматически транслитерировано "
        "%r -> %r — источник (name=%r, firstname=%r, lastname=%r, "
        "display_name=%r) не дал чистой кириллицы ни по одному полю; при "
        "необходимости поправить вручную в админке",
        entity_label, entity_data.get("id"), latin_source, transliterated,
        name, firstname, lastname, display_name,
    )
    return _split_name(transliterated)


# Группировка игроков одной строки формации (formation_field "row:col") в
# зоны L/C/R/LC/RC по количеству игроков в строке — ЭВРИСТИКА, не поле от
# API. Совпадает по духу со схемой C/L/R/LC/RC, которую уже использует KFF-
# импортёр (lineups/models.py::MatchLineupPlayer.field_position), но для
# Sportmonks не подтверждена на разных формациях/числе защитников — ОБЯЗАТЕЛЬНО
# сверить визуально на странице матча после первого живого импорта (см. план,
# фаза 3: "тестировать на реальных составах перед тем, как доверять формации").
_ZONE_BY_ROW_SIZE: Dict[int, List[str]] = {
    1: ["C"],
    2: ["L", "R"],
    3: ["L", "C", "R"],
    4: ["L", "LC", "RC", "R"],
    5: ["L", "LC", "C", "RC", "R"],
}

# --- события матча ---------------------------------------------------------
# Подтверждено вживую: GOAL, PENALTY, SUBSTITUTION, YELLOWCARD. Остальные —
# по документированной схеме Sportmonks, НЕ встречены в тестовом матче (см.
# докстринг модуля, пункт 3) — не потеряют данные, если распознаны неверно
# не будут (raw сохраняется всегда), но требуют проверки на реальном примере.
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
}

# --- статистика матча -------------------------------------------------------
# Ключи — ПОЛНЫЙ набор developer_name, реально встреченных в statistics.type
# на тестовом матче (34 уникальных типа). Значение — колонка существующей
# модели (matches/models.py). Всё, что не попало в маппинг (в т.ч. xG — на
# Starter-плане недоступен, подтверждено ранее отдельной проверкой) — не
# теряется, целиком уходит в JSONField `raw`.
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
    # KEY_PASSES — колонка key_passes в модели уже была (изначально под
    # KFF), но у Sportmonks никогда не мапилась — просто пропущенный
    # маппинг, поле молча оставалось null. Найдено 2026-09-09 при аудите
    # доступных типов статистики Sportmonks (docs.sportmonks.com/v3/
    # definitions/types/statistics/fixture-statistics) на предмет
    # неиспользуемых полезных данных.
    "KEY_PASSES": "key_passes",
    # DANGEROUS_ATTACKS — НОВОЕ (2026-09-09, тот же аудит): у Sportmonks
    # есть реальный сигнал "давления"/темпа матча (число опасных атак —
    # атака, которая едва не завершилась голом), которого не было у KFF и
    # раньше нигде в проекте не использовалось. Хороший кандидат для
    # премиального визуального индикатора "накала матча" — см.
    # dangerous_attacks в MatchTeamStatistics и его использование в
    # templates/matches/_match_stats.html.
    "DANGEROUS_ATTACKS": "dangerous_attacks",
}

# Игроцкая статистика приходит НЕ отдельным statistics-массивом, а внутри
# lineups[].details (include=lineups.details.type) — так устроено у
# Sportmonks, проверено вживую (см. import_player_statistics).
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
    # TOUCHES — не идеальный синоним "владений мячом", но ближайший
    # доступный аналог поля possessions (см. докстринг модели); в проекте
    # это поле и раньше не имело точного определения от KFF.
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
            # У проекта на сайте ровно одна лига (КПЛ) — is_primary обязателен
            # для core/views.py::standings_preview и главной страницы (см.
            # League.is_primary докстринг). Безопасно проставлять при каждом
            # синке — save() сам снимает флаг с остальных лиг, если они вдруг
            # появятся.
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
    """team_data — элемент fixture.participants (id/name/image_path/meta) или
    ответ /teams/{id} — оба эндпоинта отдают одинаковую форму базовых полей.

    ВАЖНО (найдено вживую 2026-09-08, жалоба пользователя после первого
    прогона: "эмблемы клубов доисторические"): визуально проверено — герб
    Тобола на CDN Sportmonks (image_path) — правда старый дизайн клубного
    логотипа, не ошибка кода. Это ограничение источника.

    ИЗМЕНЕНО (2026-09-09, вопрос пользователя): раньше logo_url заполнялся
    ТОЛЬКО при первом создании записи и никогда не трогался на update —
    по аналогии с manual_override у Match. На практике это означало, что
    правки герба на стороне Sportmonks НИКОГДА не долетали бы до сайта,
    даже когда источник легитимно обновлял герб — то есть ломало обещание
    "логотипы обновятся сами" в принципе, для всех команд, а не только для
    вручную исправленных. Правильное разделение ответственности: logo_url
    — поле, целиком управляемое Sportmonks, свободно перезаписывается на
    каждом синке; если staff хочет свой герб (актуальнее/официальнее, чем
    у источника), он загружает файл в поле `logo` через админку — это
    ПОЛЕ Sportmonks-импортёр никогда не трогает, и Team.logo_display
    (teams/models.py) всегда предпочитает `logo`, если он загружен."""
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


# УДАЛЕНО (2026-09-09, решение пользователя): get_or_create_stadium() и
# вся модель Stadium убраны из проекта — venue-данные Sportmonks для КПЛ
# оказались непригодны для отображения не только из-за отдельных ошибок
# сопоставления (см. историю needs_review/"Центральный стадион Хисора" в
# git-истории), а принципиально: клубы КПЛ реально играют "домашние" матчи
# на РАЗНЫХ стадионах в РАЗНЫХ городах в течение одного сезона — модель "у
# команды один домашний стадион" не соответствует действительности лиги,
# чинить сопоставление бессмысленно. См. matches/models.py (поле stadium
# удалено) и core/models_stadium.py (модель удалена).


def get_or_create_referee(referee_data: Optional[Dict]) -> Optional[Referee]:
    """В отличие от KFF (судья — свободный текст без id, см.
    parsers/kff/importers.py::get_or_create_referee_by_name с локом и
    normalize_kz-фаззи-поиском), у Sportmonks судья — стабильная сущность со
    своим id с самого начала. Матчинг всегда по sportmonks_id, никакого
    фаззи-сравнения имён не требуется.

    ВАЖНО (найдено вживую 2026-09-08, жалоба пользователя: "судья полностью
    на латинице"): проверено прямым запросом к /referees/{id}?locale=ru —
    Sportmonks НЕ переводит имена судей вообще, "name" всегда латиница
    независимо от locale (в отличие от игроков — там перевод есть почти
    всегда). Это ограничение источника, не наш баг. На CREATE заполняем
    латиницей (лучше, чем пусто); но если существующая запись уже есть —
    first_name/last_name НЕ трогаем вообще, чтобы не затирать ручную
    Cyrillic-правку staff в админке при каждом повторном синке (тот же
    принцип, что у get_or_create_team::logo_url выше)."""
    if not referee_data:
        return None
    sm_id = referee_data.get("id")
    if sm_id is None:
        return None

    referee = Referee.objects.filter(sportmonks_id=str(sm_id)).first()
    if referee is not None:
        if not referee.is_active:
            referee.is_active = True
            referee.save(update_fields=["is_active", "updated_at"])
        return referee

    # _resolve_cyrillic_name почти всегда упадёт в fallback-ветку здесь (у
    # судей "name" не переводится вообще, см. докстринг выше) — но
    # вызывается для единообразия и на случай редкого исключения, а не
    # заново пишем разбор строки.
    first_name, last_name = _resolve_cyrillic_name(referee_data, "судьи")
    referee = Referee.objects.create(
        sportmonks_id=str(sm_id), first_name=first_name, last_name=last_name, is_active=True,
    )
    logger.info("Sportmonks: создан судья %s (sportmonks_id=%s)", referee.full_name, sm_id)
    return referee


def get_or_create_coach(coach_data: Optional[Dict], team: Optional[Team] = None) -> Optional[Coach]:
    """ВАЖНО (найдено вживую 2026-09-08, жалоба пользователя: "тренеры
    полностью на латинице"): проверено прямым запросом к
    /coaches/{id}?locale=ru — так же, как у судей (см. get_or_create_referee
    выше), Sportmonks НЕ переводит имена тренеров вообще. На CREATE
    заполняем латиницей; на существующей записи first_name/last_name НЕ
    трогаем (та же защита ручной правки, что у судей/логотипов команд) —
    только team синкается на каждый вызов, потому что смена команды тренером
    это реальный факт, который должен приходить из источника, а не
    косметическая правка написания имени."""
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
        if update_fields:
            coach.save(update_fields=update_fields + ["updated_at"])
        return coach

    first_name, last_name = _resolve_cyrillic_name(coach_data, "тренера")
    coach = Coach.objects.create(
        sportmonks_id=str(sm_id), first_name=first_name, last_name=last_name,
        is_active=True, team=team,
    )
    logger.info("Sportmonks: создан тренер %s (sportmonks_id=%s)", coach.full_name, sm_id)
    return coach


def get_or_create_player(
    player_data: Optional[Dict], team: Optional[Team] = None, number: Optional[int] = None,
    match_start_time=None,
) -> Optional[Player]:
    """player_data — вложенный объект lineups[].player. Имя разрешается через
    _resolve_cyrillic_name (см. докстринг выше "определение чистого
    кириллического текста") — единого надёжного поля у Sportmonks нет,
    пробуем несколько кандидатов и берём первый полностью кириллический.

    ИСПРАВЛЕНО (2026-09-09, жалоба пользователя со скриншотом: "Виктор
    Васин" всё ещё в текущем составе "Кайрат", хотя последний раз играл в
    2024) — раньше team/number/position ЗАТИРАЛИСЬ КАЖДЫМ вызовом
    (update_or_create, последняя запись побеждает, без учёта хронологии).
    При бэкафилле сезонов Sportmonks НЕ гарантирует хронологический порядок
    (see sync_sportmonks_season.py — порядок seasons_data берётся как есть
    из /leagues/393?include=seasons), поэтому фикстура 2024 года,
    обработанная ПОСЛЕ фикстур 2025/2026, отматывала team игрока НАЗАД к
    старому клубу — а автоматической коррекции больше нет (KFF-скрапер,
    который раньше снимал is_active у ушедших игроков, удалён из проекта,
    см. players/models.py::roster_absence_streak).

    Теперь team/number/position обновляются, ТОЛЬКО если match_start_time
    этой фикстуры >= уже известного игроку last_match_at (или он ещё не
    известен) — то есть только "вперёд по времени", независимо от порядка
    обработки при бэкафилле. Имя обновляется ВСЕГДА вне зависимости от
    хронологии — оно не привязано ко времени матча, и более удачная попытка
    распознать кириллицу не должна ждать своей очереди."""
    if not player_data:
        return None
    sm_id = player_data.get("id")
    if sm_id is None:
        return None

    first_name, last_name = _resolve_cyrillic_name(player_data, "игрока")
    existing = Player.objects.filter(sportmonks_id=str(sm_id)).only("id", "last_match_at").first()

    is_more_recent = (
        match_start_time is None
        or existing is None
        or existing.last_match_at is None
        or match_start_time >= existing.last_match_at
    )

    defaults = {
        "first_name": first_name,
        "last_name": last_name,
        "is_active": True,
    }
    if is_more_recent:
        defaults["position"] = POSITION_ID_MAP.get(player_data.get("position_id"), "")
        if team is not None:
            defaults["team"] = team
        if number is not None:
            defaults["number"] = number
        if match_start_time is not None:
            defaults["last_match_at"] = match_start_time

    player, created = Player.objects.update_or_create(sportmonks_id=str(sm_id), defaults=defaults)
    if created:
        logger.info("Sportmonks: создан игрок %s (sportmonks_id=%s)", player.full_name, sm_id)
    elif not is_more_recent:
        logger.debug(
            "Sportmonks: игрок %s (sportmonks_id=%s) — фикстура %s старше уже известного "
            "last_match_at, team/number/position НЕ тронуты (защита от отката бэкафиллом не "
            "по хронологии)",
            player.full_name, sm_id, match_start_time,
        )
    return player


# ============================================================================
# Вспомогательные функции матча
# ============================================================================

def _parse_starting_at(value: Optional[str]):
    """Sportmonks отдаёт starting_at строкой "YYYY-MM-DD HH:MM:SS" — по
    документации это UTC. ПРОВЕРИТЬ НА ПЕРВОМ ЖЕ ЖИВОМ МАТЧЕ (см. докстринг
    модуля, пункт 2): сверить отображаемое на сайте время начала с реальным
    временем матча на официальных источниках (kff.kz/публичное расписание).
    Если оно окажется уже локальным (Asia/Almaty) без сдвига — эту функцию
    нужно поправить на timezone.get_current_timezone() вместо dt_timezone.utc,
    прежде чем доверять живому расписанию."""
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
    """round.name у Sportmonks — просто номер тура строкой ("21" на тестовом
    матче) — проверено вживую. Регекс-фолбэк на случай, если когда-нибудь
    придёт составное имя (напр. "Round 21"), не гадаем заранее сверх этого."""
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
    """См. докстринг _ZONE_BY_ROW_SIZE выше — эвристика, требует проверки."""
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
            # Нестандартная по размеру строка формации (>5 в ряд) — не
            # угадываем зону, оставляем пустой field_position, а не
            # присваиваем наугад неверную сторону.
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

@transaction.atomic
def import_match_core(fixture_data: Dict, league: League, season: Season) -> Match:
    """Создаёт/обновляет базовую запись Match по sportmonks_id. Уведомления
    подписчикам о "матч только что завершился" (аналог KFF's
    notify_followers_match_activity) НЕ триггерятся отсюда — в отличие от
    KFF, где import_match_core был единственным местом, видевшим переход в
    'finished', двухуровневая live-схема Sportmonks (фаза 4) уже сама знает
    "это состояние только что изменилось" на этапе лёгкого bulk-опроса —
    там и должен жить вызов notify_followers_match_activity, здесь это было
    бы дублирующей и более дорогой проверкой.

    P0 (Codex-ревью 2026-09-09, подтверждено по коду) — ТРЕТИЙ и последний
    барьер против записи чужой лиги под видом КПЛ (первые два —
    client.py::get_livescores фильтр-параметр и explicit-проверка в
    parsers/sportmonks/tasks.py::sportmonks_update_live): если у
    fixture_data есть league_id и он не совпадает с переданным league —
    останавливаем импорт совсем, а не тихо привязываем к чужой лиге/сезону.
    Отсутствие поля league_id в ответе НЕ считается ошибкой (не все include
    его гарантируют) — тогда полагаемся на два барьера выше."""
    sm_id = str(fixture_data["id"])

    fixture_league_id = fixture_data.get("league_id")
    if fixture_league_id is not None and str(fixture_league_id) != str(league.sportmonks_id):
        raise ValueError(
            f"Fixture {sm_id}: league_id={fixture_league_id} не совпадает с ожидаемой "
            f"лигой (sportmonks_id={league.sportmonks_id}) — импорт отклонён, см. P0 "
            f"в код-ревью 2026-09-09 (защита от записи чужой лиги)"
        )

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
    if status is None:
        logger.warning(
            "Sportmonks: fixture %s — неизвестный state.developer_name=%r, "
            "не найден в STATE_MAP (parsers/sportmonks/importers.py), считаю 'scheduled'",
            sm_id, dev_name,
        )
        status = "scheduled"

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

    existing = Match.objects.filter(sportmonks_id=sm_id).only(
        "id", "manual_override", "status", "start_time", "end_time", "voting_open_until",
    ).first()

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
    }

    # Тот же guard, что в KFF-импортёре (см. parsers/kff/importers.py::
    # import_match_core) — staff мог вручную поправить статус/дату раньше,
    # чем источник данных обновился; пока manual_override включён, статус и
    # производные от даты поля не трогаем.
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
        elif existing:
            defaults["end_time"] = existing.end_time
            defaults["voting_open_until"] = existing.voting_open_until
        else:
            placeholder_start = defaults["start_time"] or timezone.now()
            defaults["voting_open_until"] = placeholder_start + timedelta(hours=48)

    match, created = Match.objects.update_or_create(sportmonks_id=sm_id, defaults=defaults)

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

    # Тренер завершённого матча — зафиксированный факт (см. тот же guard в
    # parsers/kff/importers.py::import_coaches).
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


@transaction.atomic
def import_lineups(match: Match, lineups_data: List[Dict], formations_data: Optional[List[Dict]] = None) -> bool:
    if not lineups_data:
        return False

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
            player = get_or_create_player(
                entry.get("player"), team=team, number=entry.get("jersey_number"),
                match_start_time=match.start_time,
            )
            if not player:
                return
            # Детальная позиция (см. DETAILED_POSITION_ID_MAP выше) в
            # приоритете — грубая POSITION_ID_MAP как fallback, если
            # detailedPosition не пришёл (например, скамейка запасных, или
            # старые матчи, куда include ещё не долетел, пока не
            # перезапущен бэкафилл). Ключ в самом JSON-ответе читаем
            # ЗАЩИТНО в нескольких вариантах регистра — сама Sportmonks
            # документация даёт include как "detailedPosition" (camelCase),
            # но нет гарантии, что ответ transformer'а не приведёт ключ к
            # нижнему регистру (как это происходит с другими полями, см.
            # правку от 2026-09-09 в HEAVY_FIXTURE_INCLUDE выше) — лучше
            # проверить оба варианта, чем тихо потерять данные второй раз.
            detailed = entry.get("detailedPosition") or entry.get("detailedposition") or {}
            detailed_id = detailed.get("id") if isinstance(detailed, dict) else None
            position_code = (
                DETAILED_POSITION_ID_MAP.get(detailed_id)
                or POSITION_ID_MAP.get(entry.get("position_id"), "")
            )
            if entry.get("position_id") and not detailed_id:
                # Диагностика ИМЕННО той проблемы, которая уже один раз
                # прошла незамеченной (лог отсутствовал, обнаружили только
                # по скриншоту от пользователя) — теперь если include не
                # сработал, это будет видно в логах при каждом импорте, а
                # не только по факту "почему на схеме позиций одни буквы".
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

        any_saved = True

    if any_saved and not match.has_lineup:
        match.has_lineup = True
        match.save(update_fields=["has_lineup", "updated_at"])
    return any_saved


@transaction.atomic
def import_events(match: Match, events_data: List[Dict]) -> bool:
    """Не удаляет "пропавшие" события (в отличие от KFF-варианта с
    replace_existing=True) — на платном API нет наблюдавшегося у KFF паттерна
    "обрезанный/неполный ответ", и матч по sportmonks_id обновляется идемпотентно:
    повторный вызов на том же наборе событий просто обновит совпавшие по
    (минута, тип, сторона) записи на месте, не создавая дублей и не трогая id
    (а значит и EventReaction) уже сохранённых событий."""
    if not events_data:
        return False

    home_sm_id = str(match.home_team.sportmonks_id or "")
    away_sm_id = str(match.away_team.sportmonks_id or "")

    existing_pool: Dict[tuple, List[MatchEvent]] = {}
    for ev in match.events.all():
        key = (ev.minute, ev.event_type, ev.team_side)
        existing_pool.setdefault(key, []).append(ev)

    created_count = updated_count = skipped_count = 0

    for evt in events_data:
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
            # player_id = вышедший НА поле, related_player_id = ушедший с
            # поля — проверено вживую на реальном матче (см. докстринг модуля).
            if player_sm_id:
                player = Player.objects.filter(sportmonks_id=str(player_sm_id)).first()
            if related_sm_id:
                player_out = Player.objects.filter(sportmonks_id=str(related_sm_id)).first()
            if player:
                MatchLineupPlayer.objects.filter(lineup__match=match, player=player).update(
                    minute_in=minute, is_starting=False
                )
            if player_out:
                MatchLineupPlayer.objects.filter(lineup__match=match, player=player_out).update(
                    minute_out=minute
                )
        else:
            if player_sm_id:
                player = Player.objects.filter(sportmonks_id=str(player_sm_id)).first()
            if event_type in ("goal", "penalty", "own_goal") and related_sm_id:
                assist_player = Player.objects.filter(sportmonks_id=str(related_sm_id)).first()

        matched_key = (minute, event_type, team_side)
        bucket = existing_pool.get(matched_key)
        matched_existing = bucket.pop(0) if bucket else None

        if matched_existing is not None:
            matched_existing.player = player
            matched_existing.added_time = added_time
            matched_existing.assist_player = assist_player
            matched_existing.score_after = score_after
            matched_existing.player_out = player_out
            matched_existing.extra_data = evt
            matched_existing.save()
            updated_count += 1
        else:
            MatchEvent.objects.create(
                match=match,
                player=player,
                minute=minute,
                added_time=added_time,
                event_type=event_type,
                team_side=team_side,
                assist_player=assist_player,
                score_after=score_after,
                player_out=player_out,
                extra_data=evt,
            )
            created_count += 1

    logger.info(
        "Sportmonks: события матча %s — %s новых, %s обновлено, %s пропущено (неизв. тип)",
        match.id, created_count, updated_count, skipped_count,
    )
    return bool(created_count or updated_count)


@transaction.atomic
def import_statistics(match: Match, fixture_statistics: List[Dict]) -> bool:
    """fixture_statistics — командная статистика (fixture.statistics,
    participant_id без player_id). Игровая — отдельно, см.
    import_player_statistics (данные приходят внутри lineups[].details)."""
    if not fixture_statistics:
        return False
    home_sm_id = str(match.home_team.sportmonks_id or "")
    away_sm_id = str(match.away_team.sportmonks_id or "")

    by_team: Dict[str, Dict] = {}
    for entry in fixture_statistics:
        dev_name = (entry.get("type") or {}).get("developer_name")
        field = TEAM_STAT_DEV_NAME_MAP.get(dev_name)
        if not field:
            continue
        participant_id = str(entry.get("participant_id") or "")
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
        defaults["raw"] = dict(fields)
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
        player = Player.objects.filter(sportmonks_id=str(player_sm_id)).first()
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
        for d in details:
            dev_name = (d.get("type") or {}).get("developer_name")
            field = PLAYER_STAT_DEV_NAME_MAP.get(dev_name)
            if field:
                fields[field] = _stat_value(d)

        if not fields:
            continue
        defaults = dict(fields)
        defaults["team"] = team
        defaults["raw"] = dict(fields)
        MatchPlayerStatistics.objects.update_or_create(match=match, player=player, defaults=defaults)
        saved += 1

    logger.info("Sportmonks: игровая статистика матча %s — %s игрок(ов)", match.id, saved)
    return bool(saved)


# --- недоступность игроков (фаза 5) --------------------------------------
# Точные имена полей sidelined-объекта НЕ подтверждены отдельным вживую-
# запросом в ЭТОЙ сессии (в отличие от карты событий/статистики выше,
# которые сверены прямым HTTP-вызовом на реальном матче) — маппинг ниже
# написан по документированной схеме Sportmonks (team.sidelined,
# include=sidelined.player) и защитно читает несколько альтернативных путей
# до нужного значения (entry.category ИЛИ entry.sideline.category и т.д.).
# Docstring players.models.PlayerSidelined фиксирует, что более ранняя
# проверка ЖИВЫМ тестовым ключом нашла реальную запись (1 игрок Тобола,
# 07.09-14.09.2026) — структура рабочая, но перед первым боевым прогоном
# sportmonks_sync_sidelined стоит свериться на логах: если category_raw
# не встречается в SIDELINED_CATEGORY_MAP, warning в логе укажет на это
# явно, данные всё равно сохранятся как category='other', ничего не потеряется.
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


@transaction.atomic
def import_sidelined(team: Team, sidelined_data: List[Dict]) -> int:
    """Дисквалификации/травмы игроков КОНКРЕТНОЙ команды (вызывается раз на
    команду — client.get_sidelined(team.sportmonks_id), см.
    parsers/sportmonks/tasks.py::sportmonks_sync_sidelined). Игроков, которых
    ещё нет в базе (sportmonks_id не найден), пропускает — их заводит
    основной импорт состава/матчей, а не эта задача.

    Разрешившиеся случаи (снята дисквалификация/долечился) больше не
    приходят в ответе API — в отличие от исторических данных матчей, это
    ТЕКУЩИЙ статус (см. докстринг players.models.PlayerSidelined: "просто
    временный статус доступности"), поэтому старые записи для игроков этой
    команды, пропавшие из свежего ответа, удаляются, а не копятся вечно."""
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

        player = Player.objects.filter(sportmonks_id=str(player_sm_id)).first()
        if player is None:
            logger.info(
                "Sportmonks: sidelined id=%s -> player sportmonks_id=%s не найден в базе, пропуск",
                sm_id, player_sm_id,
            )
            continue

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
    """Точка входа для "тяжёлого" импорта одного матча (fixture_data — ответ
    client.get_fixture(fixture_id, include=HEAVY_FIXTURE_INCLUDE)). Вызывается
    из parsers/sportmonks/tasks.py (фаза 4) только для матчей, где
    двухуровневая схема обнаружила реальное изменение, и из бэкафилл-команды
    (sync_sportmonks_season) для истории — никогда в цикле лёгкого опроса."""
    match = import_match_core(fixture_data, league=league, season=season)
    import_coaches(match, fixture_data.get("coaches") or [])
    import_lineups(match, fixture_data.get("lineups") or [], fixture_data.get("formations") or [])
    import_events(match, fixture_data.get("events") or [])
    import_statistics(match, fixture_data.get("statistics") or [])
    import_player_statistics(match, fixture_data.get("lineups") or [])
    return match
