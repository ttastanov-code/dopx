# teams/services.py
"""
Извлечение "фирменных цветов" клуба из логотипа.

Зачем: hero-баннер страницы матча (templates/matches/_match_header.html)
раньше красился только по статусу матча (live/завершён/...). Продуктовый
запрос — красить шапку в цвета команд-участниц. Ручной ввод цвета для
каждой из ~20 команд лиги — лишняя рутина для админа, поэтому цвет
извлекается автоматически из уже загруженного логотипа (Team.logo /
Team.logo_url), а результат кэшируется в Team.primary_color /
Team.secondary_color (teams/models.py). Пересчёт — через management-
команду teams/management/commands/compute_team_colors.py, автоматически
через сигнал (teams/signals.py) при сохранении новой команды, или вручную
через действие в админке TeamAdmin.

ВАЖНО (2026-08-31, второй проход по алгоритму): большинство клубных
эмблем в этой лиге — ДВУХЦВЕТНЫЕ ("Қайрат" — жёлтый+чёрный, "Ордабасы" —
голубой+белый и т.д.), а не однотонные. Первая версия алгоритма ловила
только один самый насыщенный цвет и попутно ВЫБРАСЫВАЛА почти-чёрные
пиксели как "обводку/текст" — из-за этого у чёрно-жёлтых клубов терялась
половина фирменной палитры. Текущая версия:
  1. Открыть изображение, привести к RGBA, уменьшить до миниатюры.
  2. Отбросить только прозрачные и почти-белые пиксели (это фон логотипа
     практически всегда) — чёрный БОЛЬШЕ НЕ отбрасывается, он часто и
     есть настоящий фирменный цвет.
  3. Квантовать оставшиеся пиксели (шаг 24 на канал), чтобы сгладить шум
     сглаживания краёв, посчитать частоту каждой корзины.
  4. primary_color — самая частая корзина (это и есть доминирующий цвет
     эмблемы, каким бы он ни был — жёлтый, чёрный, синий).
  5. secondary_color — следующая по частоте корзина, которая одновременно
     (а) занимает не менее _MIN_SECONDARY_SHARE от всех учтённых пикселей
     (чтобы не подхватить редкие пиксели сглаживания как "второй цвет")
     и (б) отличается от primary на достаточное евклидово расстояние в
     RGB (чтобы не подхватить чуть более тёмный/светлый оттенок ТОГО ЖЕ
     цвета как второй). Если такой корзины нет — secondary_color = None,
     логотип действительно однотонный.

Открытие/валидация файла — по образцу users/forms.py::clean_avatar()
(Image.open + обёрнуто в try/except на случай битого файла). Скачивание
logo_url — с теми же заголовками, что и parsers/kff/photo_scraper.py
(kffleague.kz отдаёт 403 без правдоподобного User-Agent).
"""
import logging
import math
from io import BytesIO

import requests
from PIL import Image, UnidentifiedImageError

logger = logging.getLogger(__name__)

# Сетевой таймаут на скачивание логотипа по logo_url.
_FETCH_TIMEOUT = 8

# Те же заголовки, что и в parsers/kff/photo_scraper.py::_session() —
# kffleague.kz отдаёт 403 Forbidden на запросы без правдоподобного
# User-Agent (простая защита от ботов), логотипы команд (logo_url) лежат
# на том же домене (kffleague.kz/storage/qfl-files/...), поэтому нужен
# тот же обход.
_FETCH_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
}

# Шаг квантования канала (0-255) — цвета в пределах одного "ведра"
# считаются одинаковыми, чтобы сгладить шум сглаживания краёв логотипа.
_QUANT_STEP = 24

# Размер миниатюры для анализа — точность пиксель-в-пиксель не нужна.
_THUMBNAIL_SIZE = (96, 96)

# Пороги отсечения "нецветных" пикселей. Чёрный сознательно НЕ
# отбрасывается (см. докстринг модуля) — для клубов вроде "Қайрат" это
# реальный фирменный цвет, а не просто обводка/текст.
_MIN_ALPHA = 32          # почти прозрачные — не считаем
_WHITE_THRESHOLD = 235   # R,G,B все выше — считаем почти белым (фон)

# secondary_color должен покрывать не менее этой доли учтённых пикселей —
# иначе это шум сглаживания краёв, а не второй фирменный цвет.
_MIN_SECONDARY_SHARE = 0.05

# Минимальное евклидово расстояние в RGB (0..441) между primary и
# secondary — иначе это просто более тёмный/светлый оттенок того же
# цвета, а не второй цвет палитры.
_MIN_DISTINCT_DISTANCE = 70

# Сколько самых частых корзин рассматриваем при поиске secondary.
_TOP_BUCKETS = 10


def _quantize(value: int) -> int:
    return min(255, (value // _QUANT_STEP) * _QUANT_STEP + _QUANT_STEP // 2)


def _rgb_distance(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def _to_hex(rgb: tuple[int, int, int]) -> str:
    r, g, b = rgb
    return '#{:02x}{:02x}{:02x}'.format(r, g, b)


def _load_image_bytes(team) -> bytes | None:
    """Возвращает сырые байты логотипа команды или None, если недоступен."""
    if team.logo:
        try:
            team.logo.open('rb')
            try:
                return team.logo.read()
            finally:
                team.logo.close()
        except (OSError, ValueError) as exc:
            logger.warning('teams.services: не удалось прочитать logo команды %s (id=%s): %s', team.name, team.id, exc)

    if team.logo_url:
        try:
            response = requests.get(team.logo_url, headers=_FETCH_HEADERS, timeout=_FETCH_TIMEOUT)
            response.raise_for_status()
            return response.content
        except requests.RequestException as exc:
            logger.warning('teams.services: не удалось скачать logo_url команды %s (id=%s): %s', team.name, team.id, exc)

    return None


def extract_team_colors(team) -> tuple[str | None, str | None]:
    """
    Извлекает фирменную палитру команды из логотипа: (primary, secondary).

    primary — доминирующий цвет эмблемы (HEX или None, если логотип
    недоступен/повреждён). secondary — второй по значимости отчётливый
    цвет (HEX или None, если эмблема фактически однотонная либо второй
    цвет не набрал минимальную долю/отличие от primary — см. докстринг
    модуля).
    """
    raw = _load_image_bytes(team)
    if not raw:
        return None, None

    try:
        image = Image.open(BytesIO(raw))
        image.verify()
        # verify() "сжигает" файловый объект — открываем заново для чтения пикселей.
        image = Image.open(BytesIO(raw))
        image = image.convert('RGBA')
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        logger.warning('teams.services: логотип команды %s (id=%s) — не изображение или битый файл: %s', team.name, team.id, exc)
        return None, None

    image.thumbnail(_THUMBNAIL_SIZE)

    buckets: dict[tuple[int, int, int], int] = {}
    total = 0
    for r, g, b, a in image.getdata():
        if a < _MIN_ALPHA:
            continue
        if r >= _WHITE_THRESHOLD and g >= _WHITE_THRESHOLD and b >= _WHITE_THRESHOLD:
            continue
        key = (_quantize(r), _quantize(g), _quantize(b))
        buckets[key] = buckets.get(key, 0) + 1
        total += 1

    if not buckets or total == 0:
        return None, None

    ranked = sorted(buckets.items(), key=lambda item: item[1], reverse=True)[:_TOP_BUCKETS]

    primary_rgb, _primary_count = ranked[0]
    primary_hex = _to_hex(primary_rgb)

    secondary_hex = None
    for rgb, count in ranked[1:]:
        if count / total < _MIN_SECONDARY_SHARE:
            break  # дальше по списку только реже — можно остановиться
        if _rgb_distance(rgb, primary_rgb) >= _MIN_DISTINCT_DISTANCE:
            secondary_hex = _to_hex(rgb)
            break

    return primary_hex, secondary_hex
