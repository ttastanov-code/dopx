# core/services/share_cards.py
"""
Рендерим PNG на лету при первом запросе и кэшируем результат в MEDIA по
детерминированному хэшу контента — не генерируем карточки для всех матчей
заранее по крону: шерят малую долю оценок, предрендер всего — трата
CPU/диска впустую.

ШРИФТЫ: сайт в остальных местах использует Inter (webfont с Google Fonts
CDN), но Pillow не умеет рисовать текст веб-шрифтом — нужен локальный
.ttf-файл. Настоящих файлов Inter-*.ttf в репозитории нет и добавить их
сейчас неоткуда (нет сетевого доступа для скачивания). Используем
Liberation Sans — метрически близкий к Arial/Helvetica шрифт, уже
использовавшийся в проекте как замена Inter при генерации favicon/лого
(см. `static/img/dopx-logo.png`). Если/когда в репозиторий добавят
настоящие `static/fonts/Inter-Bold.ttf` и `Inter-Regular.ttf` — достаточно
поменять две строки в `_font()` ниже, остальной код не завязан на
конкретный файл.
"""
from __future__ import annotations

import hashlib
import math
from io import BytesIO
from pathlib import Path

from django.conf import settings
from django.core.files.storage import default_storage
from PIL import Image, ImageColor, ImageDraw, ImageFilter, ImageFont

CARD_SIZE = (1200, 630)  # стандартный OG-image размер
# Карточка достижения (build_badge_share_card, ниже) — единственная в этом
# модуле портретная: сделана под шеринг в мессенджеры/сторис (продуктовый
# запрос 2026-09-01, "не хуже, а лучше" референсного макета пользователя),
# а не под og:image превью ссылки — поэтому у неё свой размер, отдельный от
# общего CARD_SIZE, чтобы не задевать остальные 4 функции файла.
BADGE_CARD_SIZE = (1080, 1360)
FONTS_DIR = Path(settings.BASE_DIR) / "static" / "fonts"

# Достижения (build_badge_share_card, ниже) — визуальная система "от бронзы
# к легендарному": ПОЛНЫЙ РЕДИЗАЙН 2026-09-01 (первая версия с плоским
# кружком-монограммой была признана "скучной, дизайн бедный" — пользователь
# прислал референс премиального макета с гранёным кристаллом/трофеем,
# лавровым венком и цитатой). top/bot — двухцветный градиент кристалла,
# ТЕ ЖЕ hex у legendary, что в static/css/badges.css (#f59e0b/#a855f7) —
# фирменный цвет должен совпадать на сайте и на шеренной карточке.
# Насыщенность/сложность возрастают по списку: bronze примитивнее и тише
# любого следующего уровня — в этом весь смысл прогрессии редкости.
BADGE_RARITY_LABELS = {
    "bronze": "БРОНЗА", "silver": "СЕРЕБРО", "gold": "ЗОЛОТО",
    "platinum": "ПЛАТИНА", "secret": "СЕКРЕТНОЕ", "legendary": "ЛЕГЕНДАРНОЕ",
}
BADGE_RARITY_META = {
    "bronze": dict(
        top=(214, 150, 85), bot=(133, 80, 35), n_sides=5, gem_h=400, gem_w=270,
        glow_alpha=45, glow_scale=0.70, base_bg=(10, 10, 10), grain=False, beam=False,
        wreath=False, sparkles=0,
    ),
    "silver": dict(
        top=(225, 228, 235), bot=(150, 155, 168), n_sides=6, gem_h=430, gem_w=290,
        glow_alpha=55, glow_scale=0.75, base_bg=(10, 10, 12), grain=False, beam=False,
        wreath=False, sparkles=0,
    ),
    "gold": dict(
        top=(255, 214, 110), bot=(198, 130, 30), n_sides=6, gem_h=470, gem_w=310,
        glow_alpha=75, glow_scale=0.80, base_bg=(10, 10, 13), grain=True, beam=False,
        wreath=True, sparkles=1,
    ),
    "platinum": dict(
        top=(220, 240, 255), bot=(90, 150, 210), n_sides=7, gem_h=510, gem_w=330,
        glow_alpha=90, glow_scale=0.85, base_bg=(10, 10, 16), grain=True, beam=True,
        wreath=True, sparkles=2,
    ),
    "secret": dict(
        top=(200, 175, 255), bot=(90, 50, 150), n_sides=6, gem_h=470, gem_w=310,
        glow_alpha=85, glow_scale=0.85, base_bg=(10, 10, 16), grain=True, beam=True,
        wreath=True, sparkles=1,
    ),
    "legendary": dict(
        top=(247, 201, 110), bot=(147, 68, 229), n_sides=7, gem_h=600, gem_w=380,
        glow_alpha=100, glow_scale=0.92, base_bg=(10, 9, 16), grain=True, beam=True,
        wreath=True, sparkles=4,
    ),
}
# Короткие флейвор-цитаты по редкости (не по конкретной ачивке — 31 своя
# цитата means продуктовый/копирайтинг объём за пределами этой задачи).
# Роль та же, что в референсном макете пользователя: премиальный акцент в
# нижней панели карточки, а не описание условия получения (оно уже есть
# отдельной строкой над карточкой).
BADGE_RARITY_QUOTES = {
    "bronze": "Каждая легенда начинается с одной оценки.",
    "silver": "Постоянство — это тоже мастерство.",
    "gold": "Точность рождается из внимания к деталям.",
    "platinum": "Дисциплина побеждает случайность.",
    "secret": "Не всё раскрывается сразу.",
    "legendary": "Прогноз — это искусство видеть невидимое.",
}


_FONT_FILES = {
    "bold": "LiberationSans-Bold.ttf",
    "regular": "LiberationSans-Regular.ttf",
    # 'italic' добавлен 2026-09-01 вместе с редизайном build_badge_share_card
    # (курсивная цитата в нижней панели) — тот же файл, что и Bold/Regular,
    # скопирован из системного пакета Liberation в репозиторий (лицензия
    # SIL OFL, свободно распространяется — те же условия, что у уже
    # существующих Bold/Regular).
    "italic": "LiberationSans-Italic.ttf",
}


def _font(name: str, size: int) -> ImageFont.FreeTypeFont:
    """
    :param name: 'bold', 'regular' или 'italic'.

    Не падает, если TTF-файла нет на диске (например, в свежем клоне
    репозитория до первого `collectstatic`) — молча откатывается на
    growing default bitmap-шрифт Pillow, чтобы генерация карточки не
    роняла запрос целиком из-за отсутствующего файла шрифта.
    """
    filename = _FONT_FILES.get(name, _FONT_FILES["regular"])
    try:
        return ImageFont.truetype(str(FONTS_DIR / filename), size)
    except OSError:
        return ImageFont.load_default(size=size)


# Второе семейство шрифтов — только для build_badge_share_card (DejaVu Sans,
# не Poppins — Poppins не прошёл проверку кириллицы через fontTools cmap).
# См. docs/adr/0011-badge-share-card-legibility.md.
_BADGE_FONT_FILES = {
    "bold": "DejaVuSans-Bold.ttf",
    "regular": "DejaVuSans.ttf",
    "italic": "DejaVuSans-Oblique.ttf",
    "cond_bold": "DejaVuSansCondensed-Bold.ttf",
    "cond": "DejaVuSansCondensed.ttf",
}


def _badge_font(name: str, size: int) -> ImageFont.FreeTypeFont:
    filename = _BADGE_FONT_FILES.get(name, _BADGE_FONT_FILES["regular"])
    try:
        return ImageFont.truetype(str(FONTS_DIR / filename), size)
    except OSError:
        return ImageFont.load_default(size=size)


def _cache_key(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:24]


def _linear_gradient(size: tuple[int, int], color_a: str, color_b: str, angle: float = 135) -> Image.Image:
    """
    Линейный градиент без numpy (её нет в зависимостях проекта, добавлять
    ради одной декоративной функции нецелесообразно) — стандартный
    Pillow-приём: строим чёрно-белый линейный градиент через
    `Image.linear_gradient`, поворачиваем на нужный угол и используем как
    альфа-маску между двумя сплошными заливками через `Image.composite`.

    `expand=True` при повороте увеличивает холст — берём центральный вырез
    нужного размера. Если после поворота холст МЕНЬШЕ целевого размера
    (типично для широких карточек 1200x630 при повороте квадрата 256x256) —
    просто растягиваем поворот на весь размер: направление градиента чуть
    исказится при неравномерном масштабировании, но для декоративного фона
    это не критично, а объект остаётся плавным без резких переходов.
    """
    base = Image.linear_gradient("L").rotate(angle, resample=Image.BICUBIC, expand=True)
    bw, bh = base.size
    left, top = (bw - size[0]) // 2, (bh - size[1]) // 2
    mask = base.crop((left, top, left + size[0], top + size[1])) if left >= 0 and top >= 0 else base.resize(size)
    return Image.composite(Image.new("RGB", size, color_b), Image.new("RGB", size, color_a), mask)


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int, max_lines: int = 2) -> list[str]:
    """
    Простой word-wrap по фактической ширине текста (Pillow не переносит
    текст сам). Обрезает по `max_lines` с многоточием на последней строке,
    чтобы длинное описание достижения не вылезало за пределы карточки.
    """
    words = text.split()
    lines: list[str] = []
    current = ""
    idx = 0
    while idx < len(words) and len(lines) < max_lines:
        candidate = f"{current} {words[idx]}".strip()
        if draw.textlength(candidate, font=font) <= max_width or not current:
            current = candidate
            idx += 1
        else:
            lines.append(current)
            current = ""
    if current:
        lines.append(current)
    if idx < len(words) and lines:  # текст не поместился целиком — обрезали
        last = lines[-1]
        while draw.textlength(last + "…", font=font) > max_width and len(last) > 1:
            last = last[:-1]
        lines[-1] = last + "…"
    return lines[:max_lines]


def _mix_rgb(c1: tuple[int, int, int], c2: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    """Линейная интерполяция между двумя RGB-цветами, t в [0, 1]."""
    return tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3))  # type: ignore[return-value]


def _clamp_rgb(c: tuple[float, float, float]) -> tuple[int, int, int]:
    return tuple(max(0, min(255, int(v))) for v in c)  # type: ignore[return-value]


def _tracked_text(draw: ImageDraw.ImageDraw, xy: tuple[float, float], text: str,
                   font: ImageFont.FreeTypeFont, fill, tracking: float = 0) -> float:
    """
    Текст с ручным трекингом (letter-spacing) — Pillow не умеет это нативно.
    Использовано для капса (DOPX, эпиграф, футер, rarity-пилюля) в
    премиальной карточке достижения: именно широкий трекинг капса даёт тот
    "дорогой" визуальный эффект даже на обычном геометрическом шрифте без
    засечек (Liberation Sans — единственный доступный в репозитории).
    :return: итоговая ширина отрисованного текста.
    """
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill)
        x += draw.textlength(ch, font=font) + tracking
    return x - xy[0]


def _tracked_text_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, tracking: float = 0) -> float:
    if not text:
        return 0
    return sum(draw.textlength(ch, font=font) for ch in text) + tracking * (len(text) - 1)


def _draw_gem(
    img: Image.Image, *, cx: int, cy_center: int, height: int, width: int,
    color_top: tuple[int, int, int], color_bot: tuple[int, int, int],
    n_sides: int, seed: int, glow_alpha: int, glow_scale: float,
) -> Image.Image:
    """
    Гранёный "кристалл-трофей" — центральная иллюстрация премиальной
    карточки достижения, полностью процедурная (без внешних .png/.svg
    ассетов — в песочнице разработки нет сети, чтобы их скачать, а
    хардкодить бинарник в репозиторий не хочется). Геометрия: вершина
    сверху, три "кольца" вершин по эллипсам убывающего/растущего радиуса
    (верхнее/среднее/нижнее), вершина снизу — треугольные грани между
    соседними кольцами дают классический low-poly кристалл.

    Освещение фасетов — направленное (имитация источника света сверху
    слева: `light = -dx*0.65 + (0.35-dy)*0.55`), а не чисто случайное:
    ранняя версия с полностью рандомным затемнением/осветлением давала
    отдельные "грязно-серые" грани там, где яркая случайная добавка ложилась
    поверх уже тёмного случайного фасета. Небольшая случайная добавка
    (`+r*0.18`) поверх направленного света оставлена для лёгкой фактуры.

    :param seed: детерминированный (НЕ основанный на времени/случайности)
        параметр вариации граней — один и тот же rarity должен давать
        визуально одинаковый кристалл у всех пользователей, а не
        "случайную" форму при каждой перегенерации кэша.
    """
    top_y = cy_center - height / 2
    bot_y = cy_center + height / 2
    upper_y = top_y + height * 0.24
    mid_y = top_y + height * 0.52
    lower_y = top_y + height * 0.80
    upper_r, mid_r, lower_r = width * 0.28, width * 0.50, width * 0.30
    squash = 0.66

    def ring(cy: float, r: float, phase: float) -> list[tuple[float, float]]:
        pts = []
        for i in range(n_sides):
            ang = phase + 2 * math.pi * i / n_sides
            jr = r * (1 + 0.05 * math.sin(i * 2.3 + seed))
            pts.append((cx + jr * math.cos(ang), cy + jr * math.sin(ang) * squash))
        return pts

    apex_top, apex_bot = (cx, top_y), (cx, bot_y)
    upper_ring = ring(upper_y, upper_r, 0.35)
    mid_ring = ring(mid_y, mid_r, 0.0)
    lower_ring = ring(lower_y, lower_r, 0.55)

    def hue_at(py: float) -> tuple[int, int, int]:
        t = max(0.0, min(1.0, (py - top_y) / height))
        return _mix_rgb(color_top, color_bot, t)

    def centroid(pts: list[tuple[float, float]]) -> tuple[float, float]:
        return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))

    facets: list[list[tuple[float, float]]] = []
    for i in range(n_sides):
        facets.append([apex_top, upper_ring[i], upper_ring[(i + 1) % n_sides]])
    for i in range(n_sides):
        p1, p2, q1, q2 = upper_ring[i], upper_ring[(i + 1) % n_sides], mid_ring[i], mid_ring[(i + 1) % n_sides]
        facets.append([p1, p2, q2])
        facets.append([p1, q2, q1])
    for i in range(n_sides):
        p1, p2, q1, q2 = mid_ring[i], mid_ring[(i + 1) % n_sides], lower_ring[i], lower_ring[(i + 1) % n_sides]
        facets.append([p1, p2, q2])
        facets.append([p1, q2, q1])
    for i in range(n_sides):
        facets.append([apex_bot, lower_ring[i], lower_ring[(i + 1) % n_sides]])

    base = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(base)
    WHITE, DARK = (255, 255, 255), (14, 10, 18)
    for idx, tri in enumerate(facets):
        cxx, cyy = centroid(tri)
        hue = hue_at(cyy)
        dx = (cxx - cx) / (width / 2 + 1e-6)
        dy = (cyy - top_y) / height
        light = (-dx * 0.65) + ((0.35 - dy) * 0.55)
        rnd = math.sin(idx * 12.9898 + seed * 78.233) * 43758.5453
        rnd = (rnd - math.floor(rnd)) * 2 - 1
        light = max(-1.0, min(1.0, light + rnd * 0.18))
        col = _mix_rgb(hue, WHITE, light * 0.6) if light >= 0 else _mix_rgb(hue, DARK, -light * 0.55)
        draw.polygon(tri, fill=_clamp_rgb(col) + (255,), outline=(10, 8, 14, 110))

    if glow_alpha:
        glow_layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
        glow_color = _mix_rgb(color_top, color_bot, 0.45)
        ImageDraw.Draw(glow_layer).ellipse(
            [cx - width * glow_scale, cy_center - height * 0.60, cx + width * glow_scale, cy_center + height * 0.60],
            fill=glow_color + (glow_alpha,),
        )
        glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(110))
        img_rgba = Image.alpha_composite(img.convert("RGBA"), glow_layer)
    else:
        img_rgba = img.convert("RGBA")

    # "Заземляющая" тень под кристаллом — без неё гем визуально парит без
    # опоры на плоском тёмном фоне.
    shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).ellipse(
        [cx - width * 0.42, bot_y - 16, cx + width * 0.42, bot_y + 40], fill=(0, 0, 0, 150),
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(24))
    img_rgba = Image.alpha_composite(img_rgba, shadow)
    img_rgba = Image.alpha_composite(img_rgba, base)
    return img_rgba.convert("RGB")


def _leaf_polygon(length: float, width: float) -> list[tuple[float, float]]:
    """Заострённый лист (почти-миндаль), центр в (0,0), острие вверх (-y)."""
    pts = []
    n = 10
    for i in range(n + 1):
        t = i / n
        pts.append((math.sin(t * math.pi) * (width / 2), -t * length))
    for i in range(n + 1):
        t = i / n
        pts.append((-math.sin(t * math.pi) * (width / 2), -(1 - t) * length))
    return pts


def _draw_laurel(img: Image.Image, cx: int, cy: int, scale: float, color: tuple[int, int, int]) -> Image.Image:
    """
    Лавровый венок — пара зеркальных вееров заострённых листьев, простая
    векторная замена настоящей иллюстрации (см. докстринг `_draw_gem` про
    отсутствие сети для внешних ассетов). Ставится рядом с `@username` в
    нижней панели карточки — как "печать" полученного достижения.
    """
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    n_leaves = 4
    for side in (-1, 1):
        for i in range(n_leaves):
            t = i / (n_leaves - 1)
            r = scale * (0.35 + t * 0.55)
            px = cx + side * r * math.sin(math.radians(20 + t * 70))
            py = cy - scale * 0.15 + r * (1 - math.cos(math.radians(20 + t * 70))) * 0.5
            leaf_pts = _leaf_polygon(length=scale * 0.42 * (1 - 0.12 * t), width=scale * 0.20 * (1 - 0.1 * t))
            rot_rad = math.radians(side * (24 + t * 50))
            cos_r, sin_r = math.cos(rot_rad), math.sin(rot_rad)
            transformed = [(px + x * cos_r - y * sin_r, py + x * sin_r + y * cos_r) for x, y in leaf_pts]
            draw.polygon(transformed, fill=color + (235,))
    img_rgba = Image.alpha_composite(img.convert("RGBA"), layer)
    return img_rgba.convert("RGB")


_BRAND_ICON_PATH = Path(settings.BASE_DIR) / "static" / "img" / "dopx-logo-icon.png"
_brand_icon_cache: dict[int, Image.Image | None] = {}


def _load_brand_mark(size: int) -> Image.Image | None:
    """
    Настоящая иконка DOPX (`static/img/dopx-logo-icon.png`, 1024x1024 RGBA)
    вместо процедурного лаврового венка (`_draw_laurel`) рядом с
    `@username` в нижней панели — второй раунд правок 2026-09-01, продуктовый
    фидбэк "какая-то хуйня которая выглядит дешево, вместо него надо наш
    логотип поставить". Готовый бренд-ассет вместо очередной векторной
    самоделки — та же логика, что и переход на AI-фоны вместо процедурного
    кристалла: где есть настоящий ассет, он всегда выигрывает у Pillow-примитива.

    Кэшируется по `size` в памяти процесса на весь его жизненный цикл — тот
    же смысл, что у py-уровневого кэша шрифтов в Pillow, лишний диск-I/O и
    ресайз на каждый вызов `build_badge_share_card` не нужны.

    :return: `None`, если файла нет на диске — тот же отказоустойчивый
        паттерн, что у `_font()`/`_load_custom_badge_background`; вызывающий
        код должен откатиться на `_draw_laurel` в этом случае.
    """
    if size in _brand_icon_cache:
        return _brand_icon_cache[size]
    try:
        icon = Image.open(_BRAND_ICON_PATH).convert("RGBA")
        icon = icon.resize((size, size), Image.LANCZOS)
    except OSError:
        icon = None
    _brand_icon_cache[size] = icon
    return icon


def _draw_sparkle(draw: ImageDraw.ImageDraw, cx: float, cy: float, r: float, color: tuple[int, int, int], alpha: int = 200) -> None:
    """Маленький 4-лучевой блик-ромб — декоративная "искра" у кристалла на
    более высоких rarity. Не unicode-символ ("✦"/"★"): у Liberation Sans нет
    этих глифов (см. докстринг модуля про emoji/tofu)."""
    pts = [
        (cx, cy - r), (cx + r * 0.22, cy - r * 0.22), (cx + r, cy), (cx + r * 0.22, cy + r * 0.22),
        (cx, cy + r), (cx - r * 0.22, cy + r * 0.22), (cx - r, cy), (cx - r * 0.22, cy - r * 0.22),
    ]
    draw.polygon(pts, fill=color + (alpha,))


def _rounded_alpha_mask(size: tuple[int, int], radius: int) -> Image.Image:
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size[0] - 1, size[1] - 1], radius=radius, fill=255)
    return mask


# Легибилити-фиксы build_badge_share_card (edge vignette + text scrim) —
# см. docs/adr/0011-badge-share-card-legibility.md.
def _edge_vignette(img: Image.Image, inset: int = 90, strength: float = 0.65) -> Image.Image:
    """
    Затемняет края/углы изображения независимо от их фактического
    содержимого — блюрим маску скруглённого прямоугольника и по ней мешаем
    оригинал с почти-чёрной подложкой. `inset` — насколько маска "отступает"
    от края (больше = темнее у самой рамки), `strength` — сила затемнения
    (1.0 — полностью заменить края почти-чёрным, 0.0 — не менять).
    """
    w, h = img.size
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle([inset, inset, w - inset, h - inset], radius=inset, fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(inset * 0.9))
    dark = Image.new("RGB", (w, h), (4, 3, 6))
    faded = Image.composite(img, dark, mask)
    return Image.blend(img, faded, alpha=strength)


def _legibility_scrim(size: tuple[int, int], start_alpha: int = 225, end_fraction: float = 0.60) -> Image.Image:
    """
    Точный (не через rotate — см. докстринг `_linear_gradient` про
    неточность этого метода на широких холстах) горизонтальный
    alpha-градиент: непрозрачно-чёрный у x=0 (левая часть карточки — весь
    текст), полностью прозрачный начиная с `end_fraction` ширины (правая
    часть — иллюстрация/кристалл, её не нужно затемнять). Строим построчно
    через `putdata` на изображении высотой 1px и растягиваем по вертикали —
    так гарантированно нет ступенчатости/бандинга от поворота.
    """
    w, h = size
    cutoff = max(1, int(w * end_fraction))
    row = [max(0, int(start_alpha * (1 - x / cutoff))) if x < cutoff else 0 for x in range(w)]
    line = Image.new("L", (w, 1))
    line.putdata(row)
    alpha = line.resize((w, h))
    scrim = Image.new("RGBA", size, (4, 3, 7, 0))
    scrim.putalpha(alpha)
    return scrim


def _shadow_text(draw: ImageDraw.ImageDraw, xy: tuple[float, float], text: str,
                  font: ImageFont.FreeTypeFont, fill, shadow_alpha: int = 170, offset: tuple[int, int] = (0, 3)) -> None:
    """Текст с тёмной смещённой "подложкой" под ним — гарантирует контраст
    независимо от яркости/пестроты фона под конкретным символом (в отличие
    от одного сплошного `_legibility_scrim`, который защищает всю зону, но не
    каждую конкретную букву у своей границы)."""
    draw.text((xy[0] + offset[0], xy[1] + offset[1]), text, font=font, fill=(0, 0, 0, shadow_alpha))
    draw.text(xy, text, font=font, fill=fill)


def _shadow_tracked_text(draw: ImageDraw.ImageDraw, xy: tuple[float, float], text: str,
                          font: ImageFont.FreeTypeFont, fill, tracking: float = 0,
                          shadow_alpha: int = 170, offset: tuple[int, int] = (0, 3)) -> float:
    _tracked_text(draw, (xy[0] + offset[0], xy[1] + offset[1]), text, font, (0, 0, 0, shadow_alpha), tracking=tracking)
    return _tracked_text(draw, xy, text, font, fill, tracking=tracking)


# Опциональные AI-сгенерированные фоны достижений (2026-09-01, продуктовый
# запрос "прям крутые карточки, не пиксельная хрень" — процедурный low-poly
# кристалл ниже визуально не дотягивает до референса пользователя, Pillow
# не умеет рендерить свет/материалы/отражения, только плоские полигоны).
# См. докстринг `_load_custom_badge_background` — если сюда положить
# bronze.png/silver.png/gold.png/platinum.png/secret.png/legendary.png,
# карточка автоматически возьмёт готовую картинку вместо процедурной.
BADGE_CARD_BACKGROUNDS_DIR = Path(settings.BASE_DIR) / "static" / "img" / "badge-cards"


def _cover_resize(img: Image.Image, target_size: tuple[int, int]) -> Image.Image:
    """
    Resize+crop "cover" (как CSS `background-size: cover`) — заполняет
    `target_size` целиком, обрезая излишек по центру, без искажения
    пропорций. Нужен для пользовательских AI-сгенерированных фонов
    произвольного размера/соотношения сторон, чтобы они не растягивались и
    не оставляли пустых полос независимо от того, какой именно размер
    вернул генератор изображений.
    """
    src_w, src_h = img.size
    target_w, target_h = target_size
    scale = max(target_w / src_w, target_h / src_h)
    new_w, new_h = round(src_w * scale), round(src_h * scale)
    resized = img.resize((new_w, new_h), Image.LANCZOS)
    left, top = (new_w - target_w) // 2, (new_h - target_h) // 2
    return resized.crop((left, top, left + target_w, top + target_h))


def _load_custom_badge_background(rarity: str) -> tuple[Image.Image, float] | None:
    """
    Ищет `static/img/badge-cards/<rarity>.png` — если файл есть, используем
    его КАК ЕСТЬ вместо процедурного фона+кристалла в `build_badge_share_card`
    (текст/панель/футер поверх рисуются одинаково в обоих случаях). Если
    файла нет — молча возвращаем `None` (та же схема отказоустойчивости, что
    у `_font()` при отсутствии .ttf) — отсутствие ассета никогда не должно
    ронять генерацию карточки.

    :return: пара (изображение, mtime файла) или `None`. mtime участвует в
        кэш-ключе карточки — если пользователь позже заменит PNG на новый
        (например, перегенерирует более удачный вариант), старые
        закэшированные карточки автоматически не переиспользуются.
    """
    path = BADGE_CARD_BACKGROUNDS_DIR / f"{rarity}.png"
    if not path.exists():
        return None
    try:
        img = Image.open(path).convert("RGB")
        mtime = path.stat().st_mtime
    except OSError:
        return None
    return _cover_resize(img, BADGE_CARD_SIZE), mtime


def build_match_share_card(
    *, home_team: str, away_team: str, home_score: int, away_score: int,
    top_player_name: str, top_player_score: float,
) -> str:
    """:return: относительный путь в MEDIA к готовому PNG."""
    key = _cache_key(home_team, away_team, str(home_score), str(away_score), top_player_name, str(top_player_score))
    relative_path = f"share-cards/match_{key}.png"
    if default_storage.exists(relative_path):
        return relative_path

    img = Image.new("RGB", CARD_SIZE, color="#0a0a0a")
    draw = ImageDraw.Draw(img)
    font_bold = _font("bold", 54)
    font_regular = _font("regular", 30)
    font_small = _font("regular", 22)

    draw.text((60, 50), "DOPX", font=font_bold, fill="#ffffff")
    draw.text((60, 190), f"{home_team} {home_score}:{away_score} {away_team}", font=font_bold, fill="#ffffff")
    # Без emoji ("⭐"): Liberation Sans не содержит emoji-глифов, Pillow
    # отрисовал бы нечитаемый "tofu"-квадрат вместо звезды.
    draw.text((60, 290), f"Лучший на поле: {top_player_name} — {top_player_score:.1f}/10", font=font_regular, fill="#60a5fa")
    draw.text((60, CARD_SIZE[1] - 50), "Голос трибун измеряем — dopx.kz", font=font_small, fill="#737373")

    buffer = BytesIO()
    img.save(buffer, format="PNG", optimize=True)
    buffer.seek(0)
    default_storage.save(relative_path, buffer)
    return relative_path


def build_streak_share_card(*, username: str, streak_type: str, streak_count: int) -> str:
    """
    Retention loop "Серии" (2026-08-21, подписи ПЕРЕСМОТРЕНЫ 2026-08-31) —
    карточка серии для шеринга в соцсети, тот же кэш-по-хэшу принцип, что у
    двух функций выше. :param streak_type: 'evaluation' | 'prediction' —
    подпись и цвет акцента РАЗНЫЕ и по смыслу, не только по цвету:

    - 'evaluation': "туров подряд" — evaluation_streak считается по турам
      чемпионата, а не по дням (см. докстринг User.evaluation_streak,
      users/models.py) — матчи бывают 1–2 дня в неделю, дневная подпись
      была бы неверна почти всегда.
    - 'prediction': "угаданных подряд" — prediction_streak считается по
      подряд идущим ВЕРНЫМ прогнозам 1X2, а не по дням активности (см.
      докстринг User.prediction_streak) — "N дней подряд" тут вводило бы в
      заблуждение (можно прогнозировать каждый день и всегда ошибаться).

    :return: относительный путь в MEDIA к готовому PNG.
    """
    if streak_type == "evaluation":
        label_line1, label_line2 = "туров подряд", "оценили матч"
    else:
        label_line1, label_line2 = "прогнозов подряд", "угадали исход"
    accent = "#60a5fa" if streak_type == "evaluation" else "#a78bfa"

    key = _cache_key(username, streak_type, str(streak_count))
    relative_path = f"share-cards/streak_{key}.png"
    if default_storage.exists(relative_path):
        return relative_path

    img = Image.new("RGB", CARD_SIZE, color="#0a0a0a")
    draw = ImageDraw.Draw(img)
    font_title = _font("bold", 40)
    font_name = _font("bold", 46)
    font_number = _font("bold", 160)
    font_label = _font("regular", 32)
    font_small = _font("regular", 22)

    draw.text((60, 50), "DOPX", font=font_title, fill="#ffffff")
    draw.text((60, 120), f"@{username}", font=font_name, fill="#a3a3a3")

    # Без emoji ("🔥") — та же причина, что в build_match_share_card: у
    # Liberation Sans нет emoji-глифов.
    number_text = str(streak_count)
    draw.text((60, 240), number_text, font=font_number, fill=accent)
    number_width = draw.textlength(number_text, font=font_number)
    draw.text((60 + number_width + 30, 320), label_line1, font=font_label, fill="#ffffff")
    draw.text((60 + number_width + 30, 365), label_line2, font=font_label, fill="#ffffff")

    draw.text((60, CARD_SIZE[1] - 50), "Голос трибун измеряем — dopx.kz", font=font_small, fill="#737373")

    buffer = BytesIO()
    img.save(buffer, format="PNG", optimize=True)
    buffer.seek(0)
    default_storage.save(relative_path, buffer)
    return relative_path


def build_round_squad_share_card(
    *, season_year: str, tour: int, player_of_round_name: str,
    player_of_round_score: float | None, dramatic_match_label: str,
) -> str:
    """
    "DOPX Лучшие тура" (продуктовый запрос 2026-08-22, по мотивам ревью Codex) —
    та же кэш-по-хэшу схема, что у трёх функций выше. Вызывается один раз
    из round_squad/services.py::recompute_round, в момент когда тур
    закрывается (is_final=True) — не по HTTP-запросу, как остальные
    карточки, поэтому кэш по хэшу тут в первую очередь защита от лишней
    перезаписи файла при повторных safety-вызовах recompute на
    уже зафиксированном туре (recompute_round при is_final=True выходит
    раньше, но кэш всё равно на месте для симметрии с остальными функциями).

    :return: относительный путь в MEDIA к готовому PNG.
    """
    score_label = f"{player_of_round_score:.1f}/10" if player_of_round_score is not None else "—"
    key = _cache_key(season_year, str(tour), player_of_round_name, score_label, dramatic_match_label)
    relative_path = f"share-cards/round_{key}.png"
    if default_storage.exists(relative_path):
        return relative_path

    img = Image.new("RGB", CARD_SIZE, color="#0a0a0a")
    draw = ImageDraw.Draw(img)
    font_title = _font("bold", 34)
    font_tour = _font("bold", 50)
    font_label = _font("regular", 26)
    font_name = _font("bold", 50)
    font_score = _font("bold", 40)
    font_small = _font("regular", 22)

    draw.text((60, 50), f"Сезон {season_year}", font=font_title, fill="#a3a3a3")
    draw.text((60, 105), f"DOPX Лучшие {tour}-го тура", font=font_tour, fill="#ffffff")

    draw.text((60, 240), "Игрок тура", font=font_label, fill="#60a5fa")
    draw.text((60, 275), player_of_round_name, font=font_name, fill="#ffffff")
    draw.text((60, 340), score_label, font=font_score, fill="#60a5fa")

    if dramatic_match_label:
        draw.text((60, 440), "Самый драматичный матч тура", font=font_label, fill="#a78bfa")
        draw.text((60, 475), dramatic_match_label, font=font_name, fill="#ffffff")

    draw.text((60, CARD_SIZE[1] - 50), "Голос трибун измеряем — dopx.kz", font=font_small, fill="#737373")

    buffer = BytesIO()
    img.save(buffer, format="PNG", optimize=True)
    buffer.seek(0)
    default_storage.save(relative_path, buffer)
    return relative_path


def build_player_season_recap_card(
    *, player_name: str, team_name: str, season_label: str,
    matches_played: int, avg_performance: float | None, goals: int,
) -> str:
    """
    Продуктовый аудит, раздел 5d ("Автогенерируемый season recap") —
    "DOPX Wrapped" для одного игрока: карточка сезонной статистики,
    сгенерированная и закэшированная по тому же принципу, что
    `build_match_share_card` выше (детерминированный хэш параметров,
    рендер только по первому запросу).

    :return: относительный путь в MEDIA к готовому PNG.
    """
    performance_label = f"{avg_performance:.1f}/10" if avg_performance is not None else "нет данных"
    key = _cache_key(player_name, team_name, season_label, str(matches_played), performance_label, str(goals))
    relative_path = f"share-cards/season_recap_{key}.png"
    if default_storage.exists(relative_path):
        return relative_path

    img = Image.new("RGB", CARD_SIZE, color="#0a0a0a")
    draw = ImageDraw.Draw(img)
    font_title = _font("bold", 46)
    font_name = _font("bold", 58)
    font_label = _font("regular", 24)
    font_stat = _font("bold", 64)

    draw.text((60, 50), f"Сезон {season_label} на DOPX", font=font_title, fill="#a78bfa")
    draw.text((60, 120), player_name, font=font_name, fill="#ffffff")
    draw.text((60, 195), team_name, font=font_label, fill="#a3a3a3")

    # Три колонки статистики — тот же макет, что "карточки цифр" на
    # anti_fraud.html/HTML-версии этой страницы, только растрированный.
    columns = [
        ("Матчей сыграно", str(matches_played)),
        ("Средний рейтинг", performance_label),
        ("Голов", str(goals)),
    ]
    col_width = (CARD_SIZE[0] - 120) // 3
    for i, (label, value) in enumerate(columns):
        x = 60 + i * col_width
        draw.text((x, 320), value, font=font_stat, fill="#60a5fa")
        draw.text((x, 400), label, font=font_label, fill="#a3a3a3")

    draw.text((60, CARD_SIZE[1] - 50), "Голос трибун измеряем — dopx.kz", font=font_label, fill="#737373")

    buffer = BytesIO()
    img.save(buffer, format="PNG", optimize=True)
    buffer.seek(0)
    default_storage.save(relative_path, buffer)
    return relative_path


def build_badge_share_card(
    *, username: str, badge_code: str, badge_name: str, badge_description: str,
    rarity: str, is_secret: bool, awarded_at,
) -> str:
    """
    Премиальная шеринг-карточка достижения — ПОЛНЫЙ РЕДИЗАЙН 2026-09-01
    поверх первой версии (плоский кружок-монограмма был признан пользователем
    "скучным, дизайн бедный"; прислан референсный премиальный макет —
    портретная карточка с гранёным кристаллом/трофеем, лавровым венком у
    имени и цитатой в нижней панели). Задача явно сформулирована как "от
    обычных по возрастанию к легендарным" — визуальная сложность нарастает
    по `BADGE_RARITY_META` монотонно: у bronze самый маленький кристалл,
    минимум граней, никакого свечения/зерна/луча/венка/искр; у legendary —
    максимум всего перечисленного разом.

    Портретный формат (`BADGE_CARD_SIZE`, НЕ общий `CARD_SIZE`) — карточка
    рассчитана на шеринг в мессенджеры и сторис, а не на og:image превью
    ссылки (в отличие от остальных функций файла).

    Слои карточки (снизу вверх): тёмный фон конкретного оттенка rarity →
    два угловых цветных свечения (GaussianBlur) → диагональный световой
    луч и зерно (grain, `Image.effect_noise`) у более высоких rarity →
    тонкая цветная "корешковая" полоса по левому краю → процедурный
    гранёный кристалл (`_draw_gem`) с собственным свечением и тенью →
    декоративные искры (`_draw_sparkle`) → текстовые блоки (бренд, rarity-
    пилюля, эпиграф, заголовок, описание) → нижняя панель (лавровый венок +
    `@username` + дата, разделитель, цитата) → футер → скруглённые прозрачные
    углы всей карточки (`_rounded_alpha_mask`) в самом конце.

    Кристалл и венок — полностью процедурная векторная графика (треугольные
    грани + направленное освещение, см. докстринг `_draw_gem`), не растеризация
    готовых иллюстраций: в песочнице разработки нет сетевого доступа, чтобы
    добавить в репозиторий готовые ассеты, а хардкодить сюда бинарные файлы
    "трофея" отдельным PR — избыточно для одной функции.

    Кэш по хэшу — тот же принцип, что и в остальных функциях модуля; хэш
    включает дату получения, чтобы разные пользователи с одинаковым именем
    ачивки не переиспользовали чужой файл (username тоже участвует).

    :param awarded_at: `UserBadge.awarded_at` — только для подписи даты на
        карточке, в бизнес-логике не участвует.
    :return: относительный путь в MEDIA к готовому PNG.
    """
    rarity = rarity if rarity in BADGE_RARITY_META else "bronze"
    meta = BADGE_RARITY_META[rarity]
    date_label = awarded_at.strftime("%d.%m.%Y") if awarded_at else ""

    custom_bg = _load_custom_badge_background(rarity)
    # mtime готового фона (если есть) — часть кэш-ключа, см. докстринг
    # `_load_custom_badge_background`: замена PNG на новый вариант не должна
    # обслуживаться из кэша со старой картинкой.
    bg_marker = f"custom-{custom_bg[1]}" if custom_bg else "procedural"
    # "v4" — версия кэша поднята вместе с третьим раундом правок (усиленный
    # `_edge_vignette` strength 0.65→0.85 + настоящий логотип DOPX вместо
    # процедурного венка, см. докстринги обоих изменений) — иначе уже
    # выпущенные карточки продолжили бы отдаваться из кэша со старым видом.
    key = _cache_key(username, badge_code, rarity, date_label, "v4", bg_marker)
    relative_path = f"share-cards/badge_{key}.png"
    if default_storage.exists(relative_path):
        return relative_path

    W, H = BADGE_CARD_SIZE
    top, bot = meta["top"], meta["bot"]

    if custom_bg is not None:
        img = custom_bg[0]
    else:
        img = Image.new("RGB", BADGE_CARD_SIZE, meta["base_bg"])

        # Два угловых свечения (верх-право тёплый/верхний цвет градиента,
        # низ-лево — нижний) — общая атмосфера карточки, независимая от
        # самого кристалла (тот рисуется поверх со своим свечением).
        glow_layer = Image.new("RGBA", BADGE_CARD_SIZE, (0, 0, 0, 0))
        gd = ImageDraw.Draw(glow_layer)
        gd.ellipse([W * 0.62 - 260, 60 - 260, W * 0.62 + 260, 60 + 260], fill=top + (45,))
        gd.ellipse([120 - 260, H - 140 - 260, 120 + 260, H - 140 + 260], fill=bot + (40,))
        glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(140))
        img = Image.alpha_composite(img.convert("RGBA"), glow_layer).convert("RGB")

        if meta["beam"]:
            beam_layer = Image.new("RGBA", BADGE_CARD_SIZE, (0, 0, 0, 0))
            bx = W * 0.72
            ImageDraw.Draw(beam_layer).polygon(
                [(bx - 70, -250), (bx + 90, -250), (bx + 560, H + 250), (bx + 400, H + 250)], fill=(255, 255, 255, 10),
            )
            beam_layer = beam_layer.filter(ImageFilter.GaussianBlur(80))
            img = Image.alpha_composite(img.convert("RGBA"), beam_layer).convert("RGB")

        if meta["grain"]:
            # Лёгкое зерно (film grain) — премиальная фактура у более высоких
            # rarity; для bronze/silver сознательно выключено (см. докстринг
            # BADGE_RARITY_META про монотонно нарастающую сложность).
            noise = Image.effect_noise(BADGE_CARD_SIZE, 16).convert("L")
            noise_rgb = Image.merge("RGB", (noise, noise, noise))
            img = Image.blend(img, noise_rgb, alpha=0.025)

        # Тонкая цветная полоса-"корешок" по левому краю — единственный
        # элемент оформления, присутствующий у ВСЕХ rarity без исключения
        # (даже bronze), чтобы карточка не выглядела голой на простом уровне.
        img.paste(Image.new("RGB", (8, H), top), (0, 0))

        gem_cy = 430
        img = _draw_gem(
            img, cx=int(W * 0.70), cy_center=gem_cy, height=meta["gem_h"], width=meta["gem_w"],
            color_top=top, color_bot=bot, n_sides=meta["n_sides"],
            seed=sum(badge_code.encode()) % 97, glow_alpha=meta["glow_alpha"], glow_scale=meta["glow_scale"],
        )

        if meta["sparkles"]:
            draw_s = ImageDraw.Draw(img, "RGBA")
            positions = [(200, 220, 7), (880, 650, 5), (300, 760, 4), (140, 560, 6)]
            for i in range(meta["sparkles"]):
                sx, sy, sr = positions[i % len(positions)]
                _draw_sparkle(draw_s, sx, sy, sr, top, alpha=180)

    # edge_vignette + legibility_scrim применяются одинаково к кастомному
    # AI-фону и к процедурному фолбэку, до любого текста. strength=0.85 (не
    # 0.65 — недостаточно на скруглённом вырезе). См.
    # docs/adr/0011-badge-share-card-legibility.md.
    img = _edge_vignette(img, inset=90, strength=0.85)
    scrim = _legibility_scrim(BADGE_CARD_SIZE, start_alpha=225, end_fraction=0.60)
    img = Image.alpha_composite(img.convert("RGBA"), scrim).convert("RGB")

    draw = ImageDraw.Draw(img, "RGBA")

    font_brand = _badge_font("cond_bold", 30)
    _shadow_tracked_text(draw, (56, 52), "DOPX", font_brand, (240, 232, 215, 255), tracking=9)

    # Rarity-пилюля справа сверху. Без символа-звёздочки/иконки — у DejaVu
    # (см. докстринг `_badge_font`) нет декоративных глифов "★"/"✦"/Tabler
    # Icons, Pillow отрисовал бы нечитаемый "tofu"-квадрат вместо них.
    # Маленький закрашенный ромб рисуем сами (полигон), а не unicode-символом.
    font_pill = _badge_font("cond_bold", 21)
    pill_label = BADGE_RARITY_LABELS.get(rarity, rarity.upper())
    pill_w = _tracked_text_width(draw, pill_label, font_pill, tracking=3) + 56
    pill_h = 44
    px0, py0 = W - 56 - pill_w, 46
    draw.rounded_rectangle([px0, py0, px0 + pill_w, py0 + pill_h], radius=pill_h // 2, outline=top + (220,), width=2, fill=(10, 9, 14, 205))
    dcx, dcy = px0 + 24, py0 + pill_h / 2
    draw.polygon([(dcx, dcy - 7), (dcx + 6, dcy), (dcx, dcy + 7), (dcx - 6, dcy)], fill=top + (255,))
    _tracked_text(draw, (px0 + 40, py0 + 12), pill_label, font_pill, (235, 230, 240), tracking=3)

    font_eyebrow = _badge_font("cond_bold", 20)
    ey_y = 420
    _shadow_tracked_text(draw, (56, ey_y), "ДОСТИЖЕНИЕ ПОЛУЧЕНО", font_eyebrow, top + (255,), tracking=4)
    draw.line([(58, ey_y + 38), (150, ey_y + 38)], fill=top + (255,), width=3)

    font_title = _badge_font("bold", 54)
    title_max_w = int(W * 0.56)
    title_lines = _wrap_text(draw, badge_name, font_title, title_max_w, max_lines=2)
    ty = ey_y + 62
    for line in title_lines:
        _shadow_text(draw, (56, ty), line, font_title, (250, 248, 252, 255), shadow_alpha=190, offset=(0, 4))
        ty += 64

    font_desc = _badge_font("regular", 25)
    desc_lines = _wrap_text(draw, badge_description, font_desc, title_max_w, max_lines=2)
    ty += 12
    for line in desc_lines:
        _shadow_text(draw, (56, ty), line, font_desc, (198, 196, 206, 255), shadow_alpha=160, offset=(0, 2))
        ty += 33

    # Нижняя панель: слева бренд-марка DOPX + @username + дата получения;
    # справа — короткая флейвор-цитата по редкости (BADGE_RARITY_QUOTES),
    # визуально отделённая тонкой вертикальной чертой.
    panel_y0, panel_y1 = H - 250, H - 120
    draw.rounded_rectangle([56, panel_y0, W - 56, panel_y1], radius=22, fill=(10, 9, 14, 205), outline=(255, 255, 255, 25), width=1)
    mid_x = (56 + W - 56) // 2
    draw.line([(mid_x, panel_y0 + 22), (mid_x, panel_y1 - 22)], fill=(255, 255, 255, 35), width=1)

    # Настоящий логотип DOPX вместо процедурного лаврового венка (второй
    # раунд правок 2026-09-01 — см. докстринг `_load_brand_mark`: венок
    # "выглядел дешево", готовый бренд-ассет полностью его заменяет).
    # `meta["wreath"]` (раньше включал/выключал венок по rarity) больше не
    # используется здесь: бренд-марка — это печать подлинности DOPX, а не
    # элемент нарастающей сложности редкости, поэтому она теперь одинаково
    # показывается на ВСЕХ карточках, включая bronze.
    mark_size = 52
    mark = _load_brand_mark(mark_size)
    mark_x, mark_y = 56 + 24, (panel_y0 + panel_y1) // 2 - mark_size // 2
    if mark is not None:
        img_rgba = img.convert("RGBA")
        img_rgba.alpha_composite(mark, (int(mark_x), int(mark_y)))
        img = img_rgba.convert("RGB")
        draw = ImageDraw.Draw(img, "RGBA")
        ux = mark_x + mark_size + 20
    elif meta["wreath"]:
        # Отказоустойчивый фолбэк, если бренд-ассет вдруг отсутствует на
        # диске (см. докстринг `_load_brand_mark`) — старый венок лучше,
        # чем пустое место.
        img = _draw_laurel(img, 56 + 58, (panel_y0 + panel_y1) // 2 - 6, 42, top)
        draw = ImageDraw.Draw(img, "RGBA")
        ux = 56 + 140
    else:
        ux = 56 + 24

    font_user = _badge_font("bold", 24)
    font_date = _badge_font("regular", 18)
    draw.text((ux, (panel_y0 + panel_y1) // 2 - 25), f"@{username}", font=font_user, fill=(240, 238, 244))
    draw.text((ux, (panel_y0 + panel_y1) // 2 + 5), f"получено {date_label}" if date_label else "", font=font_date, fill=(160, 158, 168))

    font_quote_mark = _badge_font("bold", 38)
    qx = mid_x + 30
    draw.text((qx, panel_y0 + 18), "“", font=font_quote_mark, fill=top + (200,))
    font_quote = _badge_font("italic", 20)
    quote_lines = _wrap_text(draw, BADGE_RARITY_QUOTES.get(rarity, ""), font_quote, (W - 56 - 24) - qx - 32, max_lines=3)
    qy = panel_y0 + 50
    for line in quote_lines:
        draw.text((qx + 30, qy), line, font=font_quote, fill=(210, 208, 220))
        qy += 27

    font_footer = _badge_font("cond", 18)
    _tracked_text(draw, (56, H - 56), "ГОЛОС ТРИБУН ИЗМЕРЯЕМ", font_footer, (120, 118, 128), tracking=3)
    kz_w = _tracked_text_width(draw, "DOPX.KZ", font_footer, tracking=3)
    _tracked_text(draw, (W - 56 - kz_w, H - 56), "DOPX.KZ", font_footer, top, tracking=3)
    draw.line([(320, H - 46), (W - 56 - kz_w - 24, H - 46)], fill=(255, 255, 255, 25), width=1)

    # Тонкая полупрозрачная белая рамка-фаска чуть внутри края — финальный
    # штрих премиальной полировки, добавлен вместе со вторым раундом правок.
    draw.rounded_rectangle([2, 2, W - 3, H - 3], radius=34, outline=(255, 255, 255, 25), width=1)

    # Скруглённые прозрачные углы у ВСЕЙ карточки — последний шаг: карточка
    # не служит og:image (в отличие от остальных 4 функций файла), а
    # открывается напрямую/шарится через Web Share API (см.
    # templates/users/badge_catalog.html), поэтому прозрачность по углам не
    # ломает превью ссылок и придаёт вид "плавающей" премиальной карточки.
    mask = _rounded_alpha_mask(BADGE_CARD_SIZE, 36)
    out = Image.new("RGBA", BADGE_CARD_SIZE, (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)

    buffer = BytesIO()
    out.save(buffer, format="PNG", optimize=True)
    buffer.seek(0)
    default_storage.save(relative_path, buffer)
    return relative_path
