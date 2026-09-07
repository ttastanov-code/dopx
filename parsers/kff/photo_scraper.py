# parsers/kff/photo_scraper.py
"""
Скрапинг фото игроков с публичного сайта kffleague.kz (НЕ JSON API — см.
parsers/kff/client.py, который дёргает /api/v1/...; здесь другой источник).

ПРО ID: у команд id из JSON API (Team.external_id) и id из URL публичного
сайта (Team.kff_website_id) — РАЗНЫЕ пространства, сопоставляются по
названию (см. match_teams() ниже). Результат кэшируется в
Team.kff_website_id, чтобы повторные запуски не парсили список команд
заново.

У ИГРОКОВ — НЕ ТАК, и это важно: Player.external_id (проставляется
parsers/kff/importers.py из JSON API при импорте составов матчей) и
числовой id из URL публичного сайта kffleague.kz/ru/player/<id> — ОДНО И
ТО ЖЕ число. Проверено вручную 2026-08-21: /api/v1/games/<id>/lineup дал
игрока {"id": 681, "first_name": "Қуаныш", "last_name": "Қалмұратов"}, и
https://kffleague.kz/ru/player/681 открыл СТРАНИЦУ ТОГО ЖЕ ЧЕЛОВЕКА —
"Куаныш Калмуратов". Значит для любого игрока, у которого уже есть
Player.external_id (то есть он хоть раз попал в состав импортированного
матча — подавляющее большинство), сопоставление по имени вообще не нужно:
см. Шаг 0 в match_and_fetch_players_for_team() ниже. Имя (Шаги 1/2) —
только фолбэк для тех, кто ещё ни разу не играл.

ПОЧЕМУ ИМЕНА ВООБЩЕ РАСХОДИЛИСЬ (история бага, важно для понимания кода
ниже): DOPX импортирует имена игроков из JSON API с параметром lang=kz
(client.py::get_lineup и соседние методы) — казахская орфография с
диакритикой (ә/і/қ/ғ/ң/ө/ұ/ү/һ). Скрапер ИЗНАЧАЛЬНО ходил на РУССКУЮ
версию публичного сайта (/ru/team/<id>, /ru/player/<id>) — а она у KFF
показывает имена в РУССКОЙ транслитерации, причём не всегда 1:1 совпадающей
даже по согласным (пример: id=1328 на /ru/player/1328 — "Арсен Буранчиев",
а в казахской версии ТОЙ ЖЕ страницы — "Арсен Бораншиев", что совпадает с
DOPX буква в букву). Разница выявлена и подтверждена вручную 2026-08-21
через Chrome: КЛЮЧЕВОЙ факт — у kffleague.kz язык переключается ОТСУТСТВИЕМ
или НАЛИЧИЕМ префикса /ru/ в пути, а НЕ префиксом /kz/ (тот молча
редиректит на /ru/, из-за чего первая попытка эту разницу найти дала
ложноотрицательный результат): https://kffleague.kz/player/1328 (БЕЗ
префикса) — казахская версия, https://kffleague.kz/ru/player/1328 —
русская. То же для команд: казахская /teams даёт "Ертіс"/"Жетісу"/"Алтай
Өскемен" напрямую (то же самое, что в DOPX Team.name), тогда как русская
/ru/teams — "Иртыш"/"Жетысу"/"Алтай" (отсюда раньше был нужен
TEAM_NAME_ALIASES ниже — оставлен как защитный фолбэк, но по факту больше
не должен требоваться).

ВЫВОД И ИСПРАВЛЕНИЕ: скрапер ходит на КАЗАХСКУЮ (без префикса /ru/) версию
сайта — она соответствует тому же lang=kz, из которого DOPX изначально
берёт имена, поэтому точное совпадение normalize_kz срабатывает для
подавляющего большинства игроков БЕЗ всякого fuzzy-угадывания. Fuzzy
(Шаг 2 ниже) остаётся как подстраховка на случай, если у KFF в казахской
версии тоже где-то опечатка или расхождение — такое встречается редко, но
не исключено полностью.

СТРУКТУРА СТРАНИЦ (проверено вручную через Chrome DevTools 2026-08-21,
страницы — Next.js SSR, обычный requests.get() их видит как есть, JS не
нужен):

  https://kffleague.kz/teams
      <a href="/team/{website_id}">...<img ... alt="{Название команды}" .../>...

  https://kffleague.kz/team/{website_id}?tab=squad
      <a href="/player/{website_id}">
        <article>...
          <img alt="{Имя Фамилия}" srcSet="/_next/image?url=<urlencoded CDN URL>&w=256&q=75 256w, ...">
        </article>
      </a>

Реальный файл фото — не сам src/srcSet (это прокси Next.js /_next/image),
а раскодированный параметр `url=` внутри него: обычно вида
https://kffleague.kz/storage/qfl-files/player_photos/{id}_leaderboard*.png.

СОПОСТАВЛЕНИЕ ИГРОКОВ ПО ИМЕНИ: normalize_kz решает гомографы (казахская
буква vs похожая русская — Қ/К, Ұ/У и т.д.) — актуально, например, если в
DOPX имя когда-то вбили вручную русской раскладкой. После перехода на
казахскую версию сайта (см. выше) расхождений в НАСТОЯЩЕЙ орфографии стало
на порядок меньше, но полностью гарантии нет (человеческий ввод с обеих
сторон) — поэтому если точного совпадения нет, пробуем fuzzy-сравнение
(difflib.SequenceMatcher) СРЕДИ
ОСТАВШИХСЯ игроков ЭТОЙ ЖЕ команды (маленький пул ~20-30 человек — риск
спутать двух РАЗНЫХ игроков заметно ниже, чем у судей/тренеров на весь
сайт, где так же fuzzy никогда не мержится автоматически, см.
core/management/commands/dedupe_referees_coaches.py). Два порога:
- >= FUZZY_AUTO_THRESHOLD — применяем автоматически (это тот случай,
  когда отличие — 1 символ, риск обознаться на подобном пуле пренебрежимо
  мал), помечаем в отчёте отдельно от точных совпадений, чтобы можно было
  визуально проверить;
- >= FUZZY_REVIEW_THRESHOLD, но ниже AUTO — НЕ применяем, только отчёт
  (может быть как реальная опечатка, так и просто похожая фамилия другого
  игрока) — staff проверяет глазами и при необходимости заводит запись в
  players/admin.py вручную.

ВАЖНО ПРО ПОРЯДОК РАЗБОРА FUZZY-ПАР: нельзя идти по игрокам KFF просто
по порядку со страницы и для каждого сразу забирать лучшего из ОСТАВШИХСЯ
DOPX-кандидатов — так более раннему (и объективно ХУЖЕ подходящему)
KFF-имени может достаться DOPX-игрок, который на самом деле гораздо точнее
подходит какому-то более позднему KFF-имени; когда очередь доходит до
этого более позднего (и более похожего) имени, кандидат уже "занят" и
пара не матчится вовсе, хотя её ratio был выше порога. Правильно —
построить ВСЕ пары (kff_игрок, dopx_игрок) с ratio >= FUZZY_REVIEW_THRESHOLD
СРАЗУ, отсортировать по убыванию ratio и разбирать жадно сверху: тогда
самая похожая пара во всей команде получает кандидата первой, независимо
от того, в каком порядке имена шли в HTML.
"""
from __future__ import annotations

import difflib
import logging
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from core.utils import normalize_kz

logger = logging.getLogger(__name__)

BASE_URL = "https://kffleague.kz"
# БЕЗ префикса /ru/ — казахская версия сайта, та же языковая конвенция,
# что и в DOPX (см. докстринг модуля выше, раздел "ПОЧЕМУ ИМЕНА ВООБЩЕ
# РАСХОДИЛИСЬ"). /ru/teams даёт другую орфографию некоторых названий.
TEAMS_URL = f"{BASE_URL}/teams"
SCRAPE_TIMEOUT_SECONDS = 15
SCRAPE_MAX_RETRIES = 3
SCRAPE_RETRY_DELAY_SECONDS = 1.5
# Пауза между запросами страниц команд — вежливость к чужому серверу,
# скрапер не гоняется на скорость (тот же принцип, что time.sleep(0.2) в
# KFFClient._get для JSON API).
BETWEEN_REQUESTS_DELAY_SECONDS = 0.5

# Пороги fuzzy-сравнения имён игроков (difflib.SequenceMatcher.ratio) —
# см. докстринг модуля выше ("СОПОСТАВЛЕНИЕ ИГРОКОВ ПО ИМЕНИ").
FUZZY_AUTO_THRESHOLD = 0.85
FUZZY_REVIEW_THRESHOLD = 0.65

# НОВОЕ (2026-08-31, "ушедшие игроки"): сколько подряд прогонов скрапера
# игрок должен отсутствовать в свежем составе на kffleague.kz, прежде чем
# мы автоматически снимем ему is_active (перестанет показываться в
# составе команды на teams/detail.html). НЕ 1 — единичное отсутствие
# слишком часто оказывается сбоем скрапинга/временной нестыковкой данных
# на стороне KFF, а не реальным уходом (см. докстринг check_roster_departures
# ниже). Задача запускается раз в 3 дня (dopx/settings.py::CELERY_BEAT_SCHEDULE,
# 'sync-kff-player-meta'), так что порог 2 — это ПОДТВЕРЖДЁННОЕ отсутствие
# на протяжении минимум ~6 дней, разумный баланс между "не мучить
# пользователей устаревшим составом" и "не удалить игрока, у которого
# просто была пауза".
ROSTER_ABSENCE_THRESHOLD = 2

# Защита от ложного "весь состав ушёл", если сама страница скрапилась
# неудачно (таймаут отдал частичный HTML, вёрстка сайта изменилась и т.п.)
# — команда премьер-лиги реально имеет 18+ человек в заявке, если скрап
# вернул меньше — считаем прогон НЕуспешным для целей roster-diff и не
# трогаем ничьи счётчики отсутствия в этом запуске вообще.
MIN_SANE_SQUAD_SIZE = 11


def _session() -> requests.Session:
    # Тот же набор заголовков, что у KFFClient (client.py) — сайт отдаёт
    # обрезанный/иной ответ ботам без правдоподобного User-Agent.
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    })
    return session


def _fetch_html(session: requests.Session, url: str) -> str | None:
    for attempt in range(SCRAPE_MAX_RETRIES):
        try:
            response = session.get(url, timeout=SCRAPE_TIMEOUT_SECONDS)
            response.raise_for_status()
            return response.text
        except requests.exceptions.Timeout:
            logger.warning("⏱️ photo_scraper timeout (попытка %d/%d): %s", attempt + 1, SCRAPE_MAX_RETRIES, url)
        except requests.exceptions.RequestException as e:
            logger.warning(
                "⚠️ photo_scraper запрос не удался (попытка %d/%d): %s: %s",
                attempt + 1, SCRAPE_MAX_RETRIES, type(e).__name__, e,
            )
        if attempt < SCRAPE_MAX_RETRIES - 1:
            time.sleep(SCRAPE_RETRY_DELAY_SECONDS * (attempt + 1))
    logger.error("❌ photo_scraper: все %d попытки провалились для %s", SCRAPE_MAX_RETRIES, url)
    return None


def _extract_real_photo_url(img_tag) -> str | None:
    """img.srcset (или src, если srcset нет) указывает на прокси Next.js
    (/_next/image?url=<urlencoded>&w=...&q=...) — настоящий файл лежит в
    urlencoded параметре `url`. Берём первый вариант из srcset (самый
    маленький по `w=`).

    ПРИМЕЧАНИЕ (2026-08-21): результат (ScrapedPlayer.photo_url) больше
    никуда не скачивается и не сохраняется — решили отказаться от
    автоматического импорта фото (см. core/templatetags/avatar_extras.py),
    поле оставлено просто как честный слепок того, что реально есть на
    странице KFF, вдруг снова понадобится."""
    raw = img_tag.get("srcset") or img_tag.get("src")
    if not raw:
        return None
    first_candidate = raw.split(",")[0].strip().split(" ")[0]
    if not first_candidate:
        return None
    absolute = urljoin(BASE_URL, first_candidate)
    parsed = urlparse(absolute)
    if parsed.path != "/_next/image":
        # Не через прокси — уже прямая ссылка (запасной случай, если
        # KFF когда-нибудь уберёт Next Image Optimization).
        return absolute
    qs = parse_qs(parsed.query)
    real_url = qs.get("url", [None])[0]
    return real_url


@dataclass
class ScrapedTeam:
    website_id: str
    name: str


@dataclass
class ScrapedPlayer:
    website_id: str
    name: str
    photo_url: str | None
    # Код позиции (см. players/positions.py::POSITION_LABELS), выведенный
    # из русского заголовка секции состава на публичном сайте (Вратари/
    # Защитники/Полузащитники/Нападающие) — см. _extract_position_code_map()
    # и докстринг scrape_team_squad(). None, если секцию не удалось
    # определить (например, игрок попал в "Другие игроки" — сайт сам не
    # знает его амплуа, гадать не будем).
    position_code: str | None = None


# Публичный сайт (казахская версия — см. докстринг модуля) группирует
# состав команды по амплуа казахскими заголовками секций — грубее, чем
# коды в players/positions.py::POSITION_LABELS (там есть, например,
# CB/LB/RB отдельно от общего DF), но для бэкафилла ПУСТОГО Player.position
# лучше грубый код, чем никакой: amplua "Полузащитник" на странице
# игрока/фильтрах DOPX уже полезнее "—".
POSITION_GROUP_TO_CODE: dict[str, str] = {
    "Қақпашылар": "GK",           # вратари
    "Қорғаушылар": "DF",          # защитники
    "Жартылай қорғаушылар": "MF",  # полузащитники
    "Шабуылшылар": "FW",          # нападающие
    # "Басқа ойыншылар" (= "другие игроки") сознательно не мапим — сайт
    # сам не отнёс игрока ни к одной из 4 групп, домысливать амплуа не
    # наша работа.
}


def scrape_team_list() -> list[ScrapedTeam]:
    """Список всех команд с их id на публичном сайте — источник для
    сопоставления Team.kff_website_id по названию."""
    session = _session()
    html = _fetch_html(session, TEAMS_URL)
    if html is None:
        return []

    soup = BeautifulSoup(html, "lxml")
    teams: list[ScrapedTeam] = []
    seen_ids: set[str] = set()
    for anchor in soup.select('a[href^="/team/"]'):
        href = anchor.get("href", "")
        website_id = href.rstrip("/").rsplit("/", 1)[-1]
        if not website_id.isdigit() or website_id in seen_ids:
            continue
        img = anchor.find("img")
        name = (img.get("alt") if img else "") or ""
        name = name.strip()
        if not name:
            continue
        seen_ids.add(website_id)
        teams.append(ScrapedTeam(website_id=website_id, name=name))
    return teams


def _scraped_player_from_anchor(anchor, position_code: str | None) -> ScrapedPlayer | None:
    href = anchor.get("href", "")
    website_id_player = href.rstrip("/").rsplit("/", 1)[-1]
    if not website_id_player.isdigit():
        return None
    img = anchor.find("img")
    if img is None:
        return None
    name = (img.get("alt") or "").strip()
    if not name:
        return None
    return ScrapedPlayer(
        website_id=website_id_player,
        name=name,
        photo_url=_extract_real_photo_url(img),
        position_code=position_code,
    )


def scrape_team_squad(website_id: str) -> list[ScrapedPlayer]:
    """Состав одной команды (фото + имя + id игрока на сайте + позиция).

    СТРУКТУРА СТРАНИЦЫ (проверено вручную через Chrome DevTools
    2026-08-21, КАЗАХСКАЯ версия — см. докстринг модуля): состав разбит
    на 4 секции `<section>`, каждая с заголовком `<h3>` —
    "Қақпашылар"/"Қорғаушылар"/"Жартылай қорғаушылар"/"Шабуылшылар" —
    плюс необязательная 5-я "Басқа ойыншылар" (= "Другие игроки") без
    чёткого амплуа. Карточка игрока сама по себе НЕ содержит текстовую
    метку позиции (только флаг, номер, имя) — амплуа определяется ТОЛЬКО
    принадлежностью к секции. Поэтому идём по `<section>`, для каждой
    смотрим на `<h3>` и мапим её текст через POSITION_GROUP_TO_CODE, а не
    парсим карточки плоским списком без контекста секции."""
    session = _session()
    url = f"{BASE_URL}/team/{website_id}?tab=squad"
    html = _fetch_html(session, url)
    if html is None:
        return []

    soup = BeautifulSoup(html, "lxml")
    players: list[ScrapedPlayer] = []
    seen_ids: set[str] = set()

    for section in soup.find_all("section"):
        heading = section.find("h3")
        label = heading.get_text(strip=True) if heading else None
        position_code = POSITION_GROUP_TO_CODE.get(label) if label else None
        for anchor in section.select('a[href^="/player/"]'):
            href = anchor.get("href", "")
            website_id_player = href.rstrip("/").rsplit("/", 1)[-1]
            if not website_id_player.isdigit() or website_id_player in seen_ids:
                continue
            scraped = _scraped_player_from_anchor(anchor, position_code)
            if scraped is None:
                continue
            seen_ids.add(website_id_player)
            players.append(scraped)

    # Запасной путь — если вёрстка когда-нибудь изменится и секции
    # исчезнут/переименуются, не должны молча остаться без состава
    # вообще: доберём анкоры вне секций плоским проходом, просто без
    # позиции (position_code=None — как было ДО этого изменения).
    for anchor in soup.select('a[href^="/player/"]'):
        href = anchor.get("href", "")
        website_id_player = href.rstrip("/").rsplit("/", 1)[-1]
        if not website_id_player.isdigit() or website_id_player in seen_ids:
            continue
        scraped = _scraped_player_from_anchor(anchor, None)
        if scraped is None:
            continue
        seen_ids.add(website_id_player)
        players.append(scraped)

    return players


def download_photo_bytes(photo_url: str) -> bytes | None:
    session = _session()
    for attempt in range(SCRAPE_MAX_RETRIES):
        try:
            response = session.get(photo_url, timeout=SCRAPE_TIMEOUT_SECONDS)
            response.raise_for_status()
            return response.content
        except requests.exceptions.RequestException as e:
            logger.warning(
                "⚠️ download_photo_bytes не удался (попытка %d/%d) %s: %s",
                attempt + 1, SCRAPE_MAX_RETRIES, photo_url, e,
            )
            if attempt < SCRAPE_MAX_RETRIES - 1:
                time.sleep(SCRAPE_RETRY_DELAY_SECONDS)
    return None


# ИСТОРИЧЕСКИЙ фолбэк — заведён, когда TEAMS_URL ещё указывал на /ru/teams
# (русская версия сайта давала "Иртыш"/"Жетысу"/"Алтай" вместо DOPX-шных
# "Ертіс"/"Жетісу"/"Алтай Өскемен"). После перехода TEAMS_URL на казахскую
# версию (см. докстринг модуля) эти три команды уже совпадают НАПРЯМУЮ, без
# алиаса — проверено вручную 2026-08-21. Оставлен как защитный фолбэк на
# случай, если KFF снова поменяет написание, а не потому что он всё ещё
# обязателен: удалять не срочно, но и полагаться на него больше не нужно.
TEAM_NAME_ALIASES: dict[str, str] = {
    normalize_kz("Иртыш"): normalize_kz("Ертіс"),
    normalize_kz("Жетысу"): normalize_kz("Жетісу"),
    normalize_kz("Алтай"): normalize_kz("Алтай Өскемен"),
}


def match_teams(dry_run: bool = True) -> dict:
    """Сопоставляет Team.kff_website_id по normalize_kz(name), с фолбэком
    на TEAM_NAME_ALIASES для пар, где сайты называют один клуб по-разному
    (см. константу выше). Прямое совпадение только — команд в лиге ~14-16,
    ложных срабатываний от fuzzy-матчинга здесь риска больше, чем пользы
    (в отличие от игроков, где скамейка большая, тут выборка маленькая и
    её реально проверить глазами по отчёту)."""
    from teams.models import Team

    scraped = scrape_team_list()
    dopx_teams = {normalize_kz(t.name): t for t in Team.objects.filter(is_active=True)}

    matched, unmatched_kff, already_set = [], [], []
    for st in scraped:
        key = normalize_kz(st.name)
        team = dopx_teams.get(key) or dopx_teams.get(TEAM_NAME_ALIASES.get(key, ""))
        if team is None:
            unmatched_kff.append(st.name)
            continue
        if team.kff_website_id == st.website_id:
            already_set.append(team.name)
            continue
        matched.append((team, st.website_id))
        if not dry_run:
            team.kff_website_id = st.website_id
            team.save(update_fields=["kff_website_id"])

    # Те же ключи (прямые + через алиас), что использовались выше для
    # поиска team — иначе "Ертіс"/"Жетісу"/"Алтай Өскемен" попали бы сюда
    # как "не нашли", хотя выше они уже успешно сматчились через алиас.
    scraped_keys = {normalize_kz(s.name) for s in scraped}
    scraped_keys |= {TEAM_NAME_ALIASES[k] for k in scraped_keys if k in TEAM_NAME_ALIASES}

    return {
        "matched": [(t.name, wid) for t, wid in matched],
        "already_set": already_set,
        "unmatched_kff": unmatched_kff,
        "unmatched_dopx": [
            t.name for t in Team.objects.filter(is_active=True, kff_website_id__isnull=True)
            if normalize_kz(t.name) not in scraped_keys
        ],
    }


def _best_fuzzy_match(name_key: str, candidates: dict) -> tuple[str, float] | None:
    """Лучший fuzzy-кандидат среди ОСТАВШИХСЯ (ещё не сматченных) ключей.
    :return: (ключ_кандидата, ratio) или None, если пул кандидатов пуст."""
    best_key, best_ratio = None, 0.0
    for candidate_key in candidates:
        ratio = difflib.SequenceMatcher(None, name_key, candidate_key).ratio()
        if ratio > best_ratio:
            best_key, best_ratio = candidate_key, ratio
    if best_key is None:
        return None
    return best_key, best_ratio


def match_and_fetch_players_for_team(team, dry_run: bool = True) -> dict:
    """Для ОДНОЙ команды (с уже проставленным kff_website_id): скрапит
    состав, сопоставляет игроков ТРЕМЯ УБЫВАЮЩИМИ ПО НАДЁЖНОСТИ способами
    (сузили пул до игроков ЭТОЙ ЖЕ команды — тёзки в разных клубах не
    путаются):

    0. ПО ЧИСЛОВОМУ ID (точно, без всякого сравнения строк) — см. докстринг
       модуля, раздел "СОПОСТАВЛЕНИЕ ПО ID". Покрывает почти всех, кто хоть
       раз сыграл (у них уже есть Player.external_id).
    1. Точное совпадение имени после normalize_kz — фолбэк для тех, у кого
       ещё нет external_id (никогда не выходили на поле).
    2. Глобальный fuzzy-подбор по всем оставшимся парам сразу (см.
       докстринг модуля — "ВАЖНО ПРО ПОРЯДОК РАЗБОРА FUZZY-ПАР") — крайний
       случай для реальных опечаток там, где даже имя не совпало.

    Проставляет Player.kff_website_id и бэкафиллит ПУСТУЮ Player.position.
    Фото НЕ скачивает и НЕ обрабатывает (решение 2026-08-21 — отказались от
    автоматического импорта фото, см. core/templatetags/avatar_extras.py:
    вместо фото везде показывается генеративный аватар). Если раньше сюда
    передавали download_photos=True — этот параметр убран вместе с кодом
    скачивания; фото по-прежнему можно проставить вручную через админку."""
    from players.models import Player
    from players.positions import clean_position_code

    if not team.kff_website_id:
        return {"error": f"У команды «{team.name}» не проставлен kff_website_id — сначала match_teams()"}

    scraped_players = scrape_team_squad(team.kff_website_id)
    # Снимок ДО того, как Шаг 0 переприсвоит scraped_players на
    # remaining_after_id (список сжимается по ходу матчинга) — нужен ниже
    # для sanity-проверки "скрап вообще похож на настоящий состав команды".
    scraped_squad_size = len(scraped_players)
    all_dopx_players = list(Player.objects.filter(team=team, is_active=True))
    # ID -> игрок — для Шага 0. Player.external_id проставляется парсером
    # МАТЧЕЙ (parsers/kff/importers.py::get_or_create_player) из JSON API
    # KFF и оказывается ТЕМ ЖЕ числом, что и id в URL публичного сайта
    # /ru/player/<id> (см. докстринг модуля) — то есть у любого игрока,
    # который хоть раз попал в стартовый состав или на скамейку, уже есть
    # 100%-надёжный ключ для фото, и имя вообще не нужно трогать.
    by_external_id: dict[str, "Player"] = {
        p.external_id: p for p in all_dopx_players if p.external_id
    }
    remaining_dopx = {normalize_kz(p.full_name): p for p in all_dopx_players}
    # Неизменный снимок ВСЕГО ростера команды на момент старта — нужен
    # ТОЛЬКО для диагностики (см. конец функции): remaining_dopx по ходу
    # дела мутирует (кандидаты выбывают по мере того, как их забирают
    # другие пары), а для объяснения "почему не нашли" нужно видеть
    # исходный список целиком, включая уже занятых кем-то другим.
    original_dopx_by_key = dict(remaining_dopx)

    matched, fuzzy_matched, review_candidates = [], [], []
    unmatched_kff = []
    positions_backfilled: list[tuple[str, str]] = []
    # НОВОЕ: id игроков (не имена — на случай полных тёзков в одной
    # команде) для отслеживания "ушедших" ниже, см. блок roster-diff в
    # конце функции.
    matched_player_ids: set = set()
    # candidate_key -> имя KFF-игрока, который его забрал (ID/точным/
    # fuzzy-совпадением) — нужно ТОЛЬКО для диагностики ниже: объяснить
    # "похожий кандидат был, но его увели раньше", а не молча развести
    # руками.
    claimed_by: dict[str, str] = {}

    def _apply_match(player, sp) -> None:
        if dry_run:
            return
        update_fields = []
        if player.kff_website_id != sp.website_id:
            player.kff_website_id = sp.website_id
            update_fields.append("kff_website_id")
        # Бэкафилл ПУСТОЙ позиции с публичного сайта (см. докстринг
        # scrape_team_squad про группировку по секциям) — НИКОГДА не
        # перетираем уже проставленную позицию (та обычно приходит из
        # JSON API KFF с более точным кодом типа "CB"/"DM", грубая
        # группа "DF"/"MF" с публичного сайта хуже её, а не лучше).
        if not player.position and sp.position_code:
            player.position = clean_position_code(sp.position_code)
            update_fields.append("position")
            positions_backfilled.append((player.full_name, player.position))
        if update_fields:
            player.save(update_fields=update_fields)
        time.sleep(BETWEEN_REQUESTS_DELAY_SECONDS)

    # Шаг 0: сопоставление ПО ID — самый надёжный способ, идёт первым.
    # sp.website_id (число из URL /ru/player/<id>) сравнивается напрямую с
    # Player.external_id (то же число, проставленное парсером МАТЧЕЙ из
    # JSON API) — никакого сравнения строк с именами, значит казахская vs
    # русская орфография ВООБЩЕ не имеет значения для тех, у кого есть
    # external_id. Совпавшие сразу выбывают из remaining_dopx по имени,
    # чтобы шаги 1/2 их не трогали и не пытались найти для них что-то ещё.
    remaining_after_id = []
    for sp in scraped_players:
        player = by_external_id.get(sp.website_id)
        if player is None:
            remaining_after_id.append(sp)
            continue
        key = normalize_kz(player.full_name)
        remaining_dopx.pop(key, None)
        matched.append((player.full_name, sp.website_id))
        matched_player_ids.add(player.id)
        claimed_by[key] = sp.name
        _apply_match(player, sp)
    scraped_players = remaining_after_id

    # Шаг 1: точные совпадения normalize_kz (фолбэк для тех, у кого ещё
    # нет external_id) — сразу выбывают из обоих пулов, дальше их
    # fuzzy-подбор (шаг 2) не касается.
    remaining_kff = []
    for sp in scraped_players:
        key = normalize_kz(sp.name)
        player = remaining_dopx.pop(key, None)
        if player is not None:
            matched.append((player.full_name, sp.website_id))
            matched_player_ids.add(player.id)
            claimed_by[key] = sp.name
            _apply_match(player, sp)
        else:
            remaining_kff.append(sp)

    # Шаг 2: fuzzy — ГЛОБАЛЬНО по всем оставшимся парам сразу (не по одному
    # KFF-имени за раз в порядке со страницы, см. докстринг модуля). Строим
    # все пары (kff, dopx) с ratio >= FUZZY_REVIEW_THRESHOLD, сортируем по
    # убыванию ratio, разбираем жадно сверху — так самая похожая пара во
    # всей команде забирает своего кандидата первой.
    candidate_pairs: list[tuple[float, "ScrapedPlayer", str]] = []
    for sp in remaining_kff:
        key = normalize_kz(sp.name)
        for candidate_key in remaining_dopx:
            ratio = difflib.SequenceMatcher(None, key, candidate_key).ratio()
            if ratio >= FUZZY_REVIEW_THRESHOLD:
                candidate_pairs.append((ratio, sp, candidate_key))
    candidate_pairs.sort(key=lambda t: t[0], reverse=True)

    resolved_kff_ids: set[str] = set()
    for ratio, sp, candidate_key in candidate_pairs:
        if sp.website_id in resolved_kff_ids or candidate_key not in remaining_dopx:
            # Либо для этого KFF-имени уже нашли пару (auto-match или
            # первый, самый похожий review-кандидат) выше по сортировке,
            # либо DOPX-кандидата уже забрала более похожая пара —
            # пропускаем, не перезаписываем.
            continue
        if ratio >= FUZZY_AUTO_THRESHOLD:
            player = remaining_dopx.pop(candidate_key)
            fuzzy_matched.append((player.full_name, sp.name, round(ratio, 2)))
            matched_player_ids.add(player.id)
            claimed_by[candidate_key] = sp.name
            _apply_match(player, sp)
        else:
            review_candidates.append((remaining_dopx[candidate_key].full_name, sp.name, round(ratio, 2)))
        resolved_kff_ids.add(sp.website_id)

    # Кандидата по имени называем только при ratio >= FUZZY_REVIEW_THRESHOLD
    # — _best_fuzzy_match иначе возвращает "лучшего из плохих" (ratio
    # 0.40-0.44, ФИО не похожи вообще), что вводит в заблуждение. Без
    # кандидата вообще — скорее всего игрока просто нет в выборке DOPX
    # (не is_active или другая команда), не проблема fuzzy-сравнения.
    unmatched_kff_details: list[tuple[str, str]] = []
    for sp in remaining_kff:
        if sp.website_id in resolved_kff_ids:
            continue
        unmatched_kff.append(sp.name)
        key = normalize_kz(sp.name)
        best = _best_fuzzy_match(key, original_dopx_by_key)

        if best is None or best[1] < FUZZY_REVIEW_THRESHOLD:
            no_candidate_reason = (
                f"в текущем составе DOPX этой команды нет ни одного похожего имени (лучший ratio {best[1]:.2f} "
                "— это шум difflib, а не намёк на опечатку)" if best
                else "в ростере DOPX этой команды кандидатов нет вообще"
            )
            unmatched_kff_details.append((
                sp.name,
                f"{no_candidate_reason}. Скорее всего игрока здесь нет: is_active=False, привязан к "
                "другой команде или трансфер ещё не отражён в БД — проверьте в админке, это не проблема "
                "сопоставления имён",
            ))
            continue

        candidate_key, ratio = best
        candidate_name = original_dopx_by_key[candidate_key].full_name
        if candidate_key in claimed_by:
            unmatched_kff_details.append((
                sp.name,
                f"похожий кандидат «{candidate_name}» (ratio {ratio:.2f}) уже занят парой с "
                f"«{claimed_by[candidate_key]}» — проверьте вручную, это не опечатка ли в имени",
            ))
        else:
            unmatched_kff_details.append((
                sp.name,
                f"похож на «{candidate_name}» (ratio {ratio:.2f}), но ниже порога авто-применения — "
                f"проверьте глазами, не опечатка ли",
            ))

    # НОВОЕ (2026-08-31): "ушедшие игроки" — те, кто остался в remaining_dopx
    # (не нашли ни по ID, ни по точному имени, ни по авто-fuzzy) — это
    # ровно тот же список, что уже собирался для диагностики выше
    # (unmatched_dopx), теперь используем его ещё и для отслеживания счётчика
    # roster_absence_streak. Пропускаем всю эту логику целиком, если сам
    # скрап выглядит неполным/битым (MIN_SANE_SQUAD_SIZE) — см. докстринг
    # константы выше: не хотим массово "терять" весь состав команды из-за
    # временного сбоя скрапинга страницы.
    reactivated: list[str] = []
    deactivated: list[str] = []
    absence_warnings: list[tuple[str, int]] = []
    squad_scrape_looks_valid = scraped_squad_size >= MIN_SANE_SQUAD_SIZE

    if squad_scrape_looks_valid:
        for player in all_dopx_players:
            found = player.id in matched_player_ids
            if found:
                if player.roster_absence_streak != 0:
                    if not dry_run:
                        player.roster_absence_streak = 0
                        player.save(update_fields=["roster_absence_streak"])
                    reactivated.append(player.full_name)
                continue

            new_streak = player.roster_absence_streak + 1
            if new_streak >= ROSTER_ABSENCE_THRESHOLD:
                deactivated.append(player.full_name)
                if not dry_run:
                    player.roster_absence_streak = new_streak
                    player.is_active = False
                    player.save(update_fields=["roster_absence_streak", "is_active"])
            else:
                absence_warnings.append((player.full_name, new_streak))
                if not dry_run:
                    player.roster_absence_streak = new_streak
                    player.save(update_fields=["roster_absence_streak"])
    else:
        logger.warning(
            "⚠️ photo_scraper: состав «%s» на kffleague.kz выглядит неполным (%d чел., минимум %d) — "
            "пропускаем проверку 'кто ушёл' в этом запуске, чтобы не отметить весь ростер как отсутствующий.",
            team.name, scraped_squad_size, MIN_SANE_SQUAD_SIZE,
        )

    return {
        "team": team.name,
        "matched": matched,
        "fuzzy_matched": fuzzy_matched,
        "review_candidates": review_candidates,
        "unmatched_kff": unmatched_kff,
        "unmatched_kff_details": unmatched_kff_details,
        # Всё, что осталось несопоставленным в remaining_dopx после и
        # точных, и авто-fuzzy совпадений.
        "unmatched_dopx": [p.full_name for p in remaining_dopx.values()],
        "positions_backfilled": positions_backfilled,
        # "Ушедшие игроки" (см. блок выше) — только если скрап признан валидным.
        "squad_scrape_looks_valid": squad_scrape_looks_valid,
        "scraped_squad_size": scraped_squad_size,
        "roster_deactivated": deactivated,  # игроки, у которых is_active снят в этом запуске
        "roster_absence_warnings": absence_warnings,  # (имя, счётчик) — ещё не дотянули до порога
        "roster_reactivated": reactivated,  # снова нашлись после того, как счётчик уже был > 0
    }
