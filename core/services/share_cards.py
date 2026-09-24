# core/services/share_cards.py
"""Генерация PNG-карточек для шеринга (матч, ДНК матча, серии, тур, итоги сезона, достижения).

Карточка рендерится при первом запросе и кэшируется в MEDIA по хэшу содержимого.
Шрифты — локальные TTF (Liberation Sans / DejaVu Sans): Pillow не умеет веб-шрифты.
"""
from __future__ import annotations

import hashlib
import math
from io import BytesIO
from pathlib import Path

from django.conf import settings
from django.core.files.storage import default_storage
from PIL import Image, ImageColor, ImageDraw, ImageFilter, ImageFont

CARD_SIZE = (1200, 630)  # OG-image
# Карточка достижения — портретная, под мессенджеры и сторис.
BADGE_CARD_SIZE = (1080, 1360)

# Суперсэмплинг: Pillow не сглаживает фигуры, поэтому рисуем на холсте в SS_SCALE раз
# больше и в конце уменьшаем через LANCZOS. Все «сырые» пиксельные значения
# (координаты, радиусы, толщины) в build_* функциях умножаются на S = SS_SCALE.
SS_SCALE = 3
_CARD_RENDER_SIZE = (CARD_SIZE[0] * SS_SCALE, CARD_SIZE[1] * SS_SCALE)
_BADGE_RENDER_SIZE = (BADGE_CARD_SIZE[0] * SS_SCALE, BADGE_CARD_SIZE[1] * SS_SCALE)
FONTS_DIR = Path(settings.BASE_DIR) / "static" / "fonts"

# Оформление достижений по редкости: чем выше редкость, тем богаче карточка.
# Цвета legendary совпадают с static/css/badges.css.
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
# Цитаты в нижней панели карточки — по редкости, не по конкретному достижению.
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
    "italic": "LiberationSans-Italic.ttf",
}


def _font(name: str, size: int) -> ImageFont.FreeTypeFont:
    """:param name: 'bold', 'regular' или 'italic'.
    :param size: кегль на финальной карточке (внутри умножается на SS_SCALE).
    Если TTF нет — откатывается на стандартный шрифт Pillow.
    """
    filename = _FONT_FILES.get(name, _FONT_FILES["regular"])
    try:
        return ImageFont.truetype(str(FONTS_DIR / filename), size * SS_SCALE)
    except OSError:
        return ImageFont.load_default(size=size * SS_SCALE)


# DejaVu Sans для карточки достижения — в нём есть кириллица.
_BADGE_FONT_FILES = {
    "bold": "DejaVuSans-Bold.ttf",
    "regular": "DejaVuSans.ttf",
    "italic": "DejaVuSans-Oblique.ttf",
    "cond_bold": "DejaVuSansCondensed-Bold.ttf",
    "cond": "DejaVuSansCondensed.ttf",
}


def _badge_font(name: str, size: int) -> ImageFont.FreeTypeFont:
    """:param size: кегль на финальной карточке (умножается на SS_SCALE)."""
    filename = _BADGE_FONT_FILES.get(name, _BADGE_FONT_FILES["regular"])
    try:
        return ImageFont.truetype(str(FONTS_DIR / filename), size * SS_SCALE)
    except OSError:
        return ImageFont.load_default(size=size * SS_SCALE)


def _cache_key(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:24]


def _linear_gradient(size: tuple[int, int], color_a: str, color_b: str, angle: float = 135) -> Image.Image:
    """Линейный градиент без numpy: повёрнутая маска Image.linear_gradient + composite."""
    base = Image.linear_gradient("L").rotate(angle, resample=Image.BICUBIC, expand=True)
    bw, bh = base.size
    left, top = (bw - size[0]) // 2, (bh - size[1]) // 2
    mask = base.crop((left, top, left + size[0], top + size[1])) if left >= 0 and top >= 0 else base.resize(size)
    return Image.composite(Image.new("RGB", size, color_b), Image.new("RGB", size, color_a), mask)


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int, max_lines: int = 2) -> list[str]:
    """Перенос текста по ширине с обрезкой по max_lines (многоточие в конце)."""
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
    if idx < len(words) and lines:
        last = lines[-1]
        while draw.textlength(last + "…", font=font) > max_width and len(last) > 1:
            last = last[:-1]
        lines[-1] = last + "…"
    return lines[:max_lines]


def _fit_single_line(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> str:
    """Одна строка с многоточием, если не влезает в max_width."""
    if draw.textlength(text, font=font) <= max_width:
        return text
    trimmed = text
    while trimmed and draw.textlength(trimmed + "…", font=font) > max_width:
        trimmed = trimmed[:-1]
    return (trimmed + "…") if trimmed else "…"


def _mix_rgb(c1: tuple[int, int, int], c2: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    """Линейная интерполяция между двумя RGB-цветами, t в [0, 1]."""
    return tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3))  # type: ignore[return-value]


def _clamp_rgb(c: tuple[float, float, float]) -> tuple[int, int, int]:
    return tuple(max(0, min(255, int(v))) for v in c)  # type: ignore[return-value]


def _tracked_text(draw: ImageDraw.ImageDraw, xy: tuple[float, float], text: str,
                   font: ImageFont.FreeTypeFont, fill, tracking: float = 0) -> float:
    """Текст с межбуквенным интервалом (Pillow этого не умеет). Возвращает ширину."""
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
    """Процедурный гранёный кристалл для карточки достижения.

    seed детерминированный — у одной редкости кристалл всегда одинаковый.
    Геометрические параметры уже в масштабе SS_SCALE.
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
        glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(110 * SS_SCALE))
        img_rgba = Image.alpha_composite(img.convert("RGBA"), glow_layer)
    else:
        img_rgba = img.convert("RGBA")

    # Тень под кристаллом.
    shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).ellipse(
        [cx - width * 0.42, bot_y - 16 * SS_SCALE, cx + width * 0.42, bot_y + 40 * SS_SCALE], fill=(0, 0, 0, 150),
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(24 * SS_SCALE))
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
    """Лавровый венок — fallback, если нет логотипа DOPX."""
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
    """Логотип DOPX (static/img/dopx-logo-icon.png) нужного размера, кэшируется в памяти.
    None, если файла нет — тогда рисуем _draw_laurel.
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
    """Декоративный блик-ромб (unicode-звёзд нет в шрифте)."""
    pts = [
        (cx, cy - r), (cx + r * 0.22, cy - r * 0.22), (cx + r, cy), (cx + r * 0.22, cy + r * 0.22),
        (cx, cy + r), (cx - r * 0.22, cy + r * 0.22), (cx - r, cy), (cx - r * 0.22, cy - r * 0.22),
    ]
    draw.polygon(pts, fill=color + (alpha,))


def _rounded_alpha_mask(size: tuple[int, int], radius: int) -> Image.Image:
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size[0] - 1, size[1] - 1], radius=radius, fill=255)
    return mask


# Затемнение краёв и подложка под текст — см. docs/adr/0011-badge-share-card-legibility.md.
def _edge_vignette(img: Image.Image, inset: int = 90, strength: float = 0.65) -> Image.Image:
    """Затемняет края изображения через размытую маску. strength — сила (0..1)."""
    w, h = img.size
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle([inset, inset, w - inset, h - inset], radius=inset, fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(inset * 0.9))
    dark = Image.new("RGB", (w, h), (4, 3, 6))
    faded = Image.composite(img, dark, mask)
    return Image.blend(img, faded, alpha=strength)


def _legibility_scrim(size: tuple[int, int], start_alpha: int = 225, end_fraction: float = 0.60) -> Image.Image:
    """Горизонтальный градиент-подложка: чёрный слева (текст), прозрачный с end_fraction ширины."""
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
                  font: ImageFont.FreeTypeFont, fill, shadow_alpha: int = 170,
                  offset: tuple[int, int] = (0, 3 * SS_SCALE)) -> None:
    """Текст с тёмной тенью — читается на любом фоне."""
    draw.text((xy[0] + offset[0], xy[1] + offset[1]), text, font=font, fill=(0, 0, 0, shadow_alpha))
    draw.text(xy, text, font=font, fill=fill)


def _shadow_tracked_text(draw: ImageDraw.ImageDraw, xy: tuple[float, float], text: str,
                          font: ImageFont.FreeTypeFont, fill, tracking: float = 0,
                          shadow_alpha: int = 170, offset: tuple[int, int] = (0, 3 * SS_SCALE)) -> float:
    _tracked_text(draw, (xy[0] + offset[0], xy[1] + offset[1]), text, font, (0, 0, 0, shadow_alpha), tracking=tracking)
    return _tracked_text(draw, xy, text, font, fill, tracking=tracking)


# Готовые фоны достижений: static/img/badge-cards/<rarity>.png.
# Если файла нет — рисуется процедурный фон с кристаллом.
BADGE_CARD_BACKGROUNDS_DIR = Path(settings.BASE_DIR) / "static" / "img" / "badge-cards"


def _cover_resize(img: Image.Image, target_size: tuple[int, int]) -> Image.Image:
    """Ресайз «cover» с обрезкой по центру (как CSS background-size: cover)."""
    src_w, src_h = img.size
    target_w, target_h = target_size
    scale = max(target_w / src_w, target_h / src_h)
    new_w, new_h = round(src_w * scale), round(src_h * scale)
    resized = img.resize((new_w, new_h), Image.LANCZOS)
    left, top = (new_w - target_w) // 2, (new_h - target_h) // 2
    return resized.crop((left, top, left + target_w, top + target_h))


def _load_custom_badge_background(rarity: str) -> tuple[Image.Image, float] | None:
    """Готовый фон достижения static/img/badge-cards/<rarity>.png.
    Возвращает (изображение, mtime) или None. mtime входит в ключ кэша.
    """
    path = BADGE_CARD_BACKGROUNDS_DIR / f"{rarity}.png"
    if not path.exists():
        return None
    try:
        img = Image.open(path).convert("RGB")
        mtime = path.stat().st_mtime
    except OSError:
        return None
    return _cover_resize(img, _BADGE_RENDER_SIZE), mtime


def _cover_resize_right(img: Image.Image, target_size: tuple[int, int]) -> Image.Image:
    """Как _cover_resize, но с привязкой к правому краю (иллюстрация фона справа)."""
    src_w, src_h = img.size
    target_w, target_h = target_size
    scale = max(target_w / src_w, target_h / src_h)
    new_w, new_h = round(src_w * scale), round(src_h * scale)
    resized = img.resize((new_w, new_h), Image.LANCZOS)
    left = new_w - target_w
    top = (new_h - target_h) // 2
    return resized.crop((left, top, left + target_w, top + target_h))


# Фоны карточек серий: prediction_strick.png / evaluation_strick.png
# («strick» — так названы файлы). Нет файла — плоский тёмный фон.
STREAK_CARD_BACKGROUNDS = {
    "prediction": "prediction_strick.png",
    "evaluation": "evaluation_strick.png",
}


def _load_streak_background(streak_type: str) -> tuple[Image.Image, float] | None:
    """Возвращает (фон, mtime) или None."""
    filename = STREAK_CARD_BACKGROUNDS.get(streak_type)
    if not filename:
        return None
    path = BADGE_CARD_BACKGROUNDS_DIR / filename
    if not path.exists():
        return None
    try:
        img = Image.open(path).convert("RGB")
        mtime = path.stat().st_mtime
    except OSError:
        return None
    return _cover_resize_right(img, _CARD_RENDER_SIZE), mtime


MATCH_DNA_BACKGROUND_FILENAME = "match_dna.png"


def _load_match_dna_background() -> tuple[Image.Image, float] | None:
    """Фон карточки «ДНК матча» (static/img/badge-cards/match_dna.png).
    Иллюстрация справа, поэтому обрезка с привязкой вправо. Возвращает (фон, mtime) или None.
    """
    path = BADGE_CARD_BACKGROUNDS_DIR / MATCH_DNA_BACKGROUND_FILENAME
    if not path.exists():
        return None
    try:
        img = Image.open(path).convert("RGB")
        mtime = path.stat().st_mtime
    except OSError:
        return None
    return _cover_resize_right(img, _CARD_RENDER_SIZE), mtime


def build_match_share_card(
    *, home_team: str, away_team: str, home_score: int, away_score: int,
    top_player_name: str, top_player_score: float,
) -> str:
    """Возвращает путь в MEDIA к готовому PNG."""
    key = _cache_key(home_team, away_team, str(home_score), str(away_score), top_player_name, str(top_player_score))
    relative_path = f"share-cards/match_{key}.png"
    if default_storage.exists(relative_path):
        return relative_path

    # Рисуем в масштабе SS_SCALE, в конце уменьшаем.
    S = SS_SCALE
    img = Image.new("RGB", _CARD_RENDER_SIZE, color="#0a0a0a")
    draw = ImageDraw.Draw(img)
    font_bold = _font("bold", 54)
    font_regular = _font("regular", 30)
    font_small = _font("regular", 22)

    draw.text((60 * S, 50 * S), "DOPX", font=font_bold, fill="#ffffff")
    draw.text((60 * S, 190 * S), f"{home_team} {home_score}:{away_score} {away_team}", font=font_bold, fill="#ffffff")
    # Без emoji — в шрифте их нет.
    draw.text((60 * S, 290 * S), f"Лучший на поле: {top_player_name} — {top_player_score:.1f}/10", font=font_regular, fill="#60a5fa")
    draw.text((60 * S, _CARD_RENDER_SIZE[1] - 50 * S), "Голос трибун измеряем — dopx.kz", font=font_small, fill="#737373")

    # Уменьшаем до финального размера (сглаживание).
    img = img.resize(CARD_SIZE, Image.LANCZOS)
    buffer = BytesIO()
    img.save(buffer, format="PNG", optimize=True)
    buffer.seek(0)
    default_storage.save(relative_path, buffer)
    return relative_path


def build_match_dna_share_card(
    *, home_team: str, away_team: str, home_score: int, away_score: int,
    drama_level: str, drama_index: float, hero_name: str, hero_score: float | None,
    headline: str, antihero_name: str = "", antihero_score: float | None = None,
    fan_mood_text: str = "", consensus_text: str = "",
) -> str:
    """Карточка «ДНК матча»: счёт, драма, герой/антигерой, настроение фанатов,
    расхождение мнений и главный факт (headline, выбирается во view).

    Необязательные строки просто пропускаются. Возвращает путь в MEDIA.
    """
    drama_label = {"high": "Высокая драма", "medium": "Средняя драма", "low": "Спокойный матч"}.get(drama_level, "")
    hero_label = f"{hero_name} — {hero_score:.1f}/10" if hero_name and hero_score is not None else ""
    antihero_label = f"{antihero_name} — {antihero_score:.1f}/10" if antihero_name and antihero_score is not None else ""
    # Фирменный фиолетовый — акцент бренда (цвет драмы — отдельно).
    brand_accent = (167, 139, 250)
    drama_accent = {
        "high": (248, 113, 113), "medium": (251, 191, 36), "low": (163, 163, 163),
    }.get(drama_level, (163, 163, 163))

    custom_bg = _load_match_dna_background()
    bg_marker = f"custom-{custom_bg[1]}" if custom_bg else "flat"
    key = _cache_key(
        home_team, away_team, str(home_score), str(away_score),
        drama_level, str(drama_index), hero_name, str(hero_score), headline,
        # Версия в ключе кэша — при смене дизайна старые карточки не отдаются.
        "v3", antihero_name, str(antihero_score), fan_mood_text, consensus_text, bg_marker,
    )
    relative_path = f"share-cards/match_dna_{key}.png"
    if default_storage.exists(relative_path):
        return relative_path

    S = SS_SCALE
    W, H = _CARD_RENDER_SIZE
    MARGIN = 64 * S

    img = custom_bg[0] if custom_bg is not None else Image.new("RGB", _CARD_RENDER_SIZE, (10, 10, 10))

    # Затемнение краёв + подложка слева под текст.
    img = _edge_vignette(img, inset=36 * S, strength=0.45)
    scrim = _legibility_scrim(_CARD_RENDER_SIZE, start_alpha=235, end_fraction=0.54)
    img = Image.alpha_composite(img.convert("RGBA"), scrim).convert("RGB")

    draw = ImageDraw.Draw(img, "RGBA")
    # Текстовая колонка слева, иллюстрация справа.
    text_max_w = int(W * 0.56) - MARGIN
    # Нижняя граница контента (над футером). Не влезающие блоки пропускаем целиком.
    CONTENT_BOTTOM = H - 78 * S

    # Логотип + название.
    font_brand = _badge_font("cond_bold", 24)
    logo_size = 38 * S
    logo = _load_brand_mark(logo_size)
    if logo is not None:
        img_rgba = img.convert("RGBA")
        img_rgba.alpha_composite(logo, (MARGIN, 50 * S))
        img = img_rgba.convert("RGB")
        draw = ImageDraw.Draw(img, "RGBA")
        brand_x = MARGIN + logo_size + 14 * S
    else:
        brand_x = MARGIN
    _tracked_text(draw, (brand_x, 58 * S), "DOPX", font_brand, (240, 238, 244, 255), tracking=5 * S)

    # Заголовок-эйброу.
    font_eyebrow = _badge_font("cond_bold", 19)
    ey_y = 120 * S
    _shadow_tracked_text(draw, (MARGIN, ey_y), "ДНК МАТЧА", font_eyebrow, brand_accent + (255,), tracking=4 * S)
    draw.line([(MARGIN + 2 * S, ey_y + 32 * S), (MARGIN + 90 * S, ey_y + 32 * S)], fill=brand_accent + (255,), width=3 * S)

    # Счёт — всегда одна строка.
    font_title = _badge_font("bold", 42)
    title_text = _fit_single_line(draw, f"{home_team} {home_score}:{away_score} {away_team}", font_title, text_max_w)
    y = 158 * S
    _shadow_text(draw, (MARGIN, y), title_text, font_title, (250, 248, 252, 255), shadow_alpha=190, offset=(0, 3 * S))
    y += 64 * S

    # Пилюля драмы, цвет по drama_level.
    if drama_label:
        font_pill = _badge_font("cond_bold", 20)
        pill_text = f"{drama_label} · индекс {drama_index:.0f}"
        pill_w = _tracked_text_width(draw, pill_text, font_pill, tracking=2 * S) + 40 * S
        pill_h = 42 * S
        draw.rounded_rectangle(
            [MARGIN, y, MARGIN + pill_w, y + pill_h], radius=pill_h // 2,
            outline=drama_accent + (220,), width=2 * S, fill=(10, 9, 14, 190),
        )
        _tracked_text(draw, (MARGIN + 20 * S, y + 11 * S), pill_text, font_pill, drama_accent + (255,), tracking=2 * S)
        y += pill_h + 18 * S

    # Герой/антигерой с цветной точкой-маркером.
    font_fact_label = _badge_font("cond_bold", 16)
    font_fact_value = _badge_font("bold", 24)

    def _fact_row(label: str, value: str, color: tuple[int, int, int], y_pos: float) -> float:
        dot_r = 5 * S
        dcy = y_pos + 16 * S
        draw.ellipse([MARGIN, dcy - dot_r, MARGIN + dot_r * 2, dcy + dot_r], fill=color + (255,))
        tx = MARGIN + dot_r * 2 + 14 * S
        _tracked_text(draw, (tx, y_pos), label.upper(), font_fact_label, color + (230,), tracking=2 * S)
        value_fitted = _fit_single_line(draw, value, font_fact_value, text_max_w - (tx - MARGIN))
        _shadow_text(draw, (tx, y_pos + 20 * S), value_fitted, font_fact_value, (245, 244, 248, 255), shadow_alpha=150, offset=(0, 2 * S))
        return y_pos + 48 * S

    if hero_label and y + 48 * S <= CONTENT_BOTTOM:
        y = _fact_row("Герой матча", hero_label, (96, 165, 250), y)
    if antihero_label and y + 48 * S <= CONTENT_BOTTOM:
        y = _fact_row("Антигерой", antihero_label, (248, 113, 113), y)

    # Резервируем место под headline (2 строки) — он важнее настроения и консенсуса.
    HEADLINE_RESERVE = (8 + 26 + 50) * S if headline else 0

    font_meta = _badge_font("regular", 20)
    if fan_mood_text and y + 26 * S + HEADLINE_RESERVE <= CONTENT_BOTTOM:
        y += 6 * S
        fan_lines_budget = 2 if y + 26 * 2 * S + HEADLINE_RESERVE <= CONTENT_BOTTOM else 1
        for line in _wrap_text(draw, fan_mood_text, font_meta, text_max_w, max_lines=fan_lines_budget):
            draw.text((MARGIN, y), line, font=font_meta, fill=(210, 208, 220, 255))
            y += 26 * S
        y += 4 * S
    if consensus_text and y + 24 * S + HEADLINE_RESERVE <= CONTENT_BOTTOM:
        line = _fit_single_line(draw, consensus_text, font_meta, text_max_w)
        draw.text((MARGIN, y), line, font=font_meta, fill=(160, 158, 168, 255))
        y += 28 * S

    # Headline — цитатой с акцентной кавычкой.
    if headline and y + 40 * S <= CONTENT_BOTTOM:
        y += 8 * S
        font_quote_mark = _badge_font("bold", 32)
        draw.text((MARGIN, y), "“", font=font_quote_mark, fill=brand_accent + (200,))
        font_quote = _badge_font("italic", 20)
        quote_lines = _wrap_text(draw, headline, font_quote, text_max_w - 28 * S, max_lines=2)
        qy = y + 26 * S
        for line in quote_lines:
            draw.text((MARGIN + 28 * S, qy), line, font=font_quote, fill=(224, 222, 232, 255))
            qy += 25 * S

    # Футер.
    font_footer = _badge_font("cond", 16)
    _tracked_text(draw, (MARGIN, H - 52 * S), "ГОЛОС ТРИБУН ИЗМЕРЯЕМ", font_footer, (170, 168, 178, 255), tracking=3 * S)
    kz_w = _tracked_text_width(draw, "DOPX.KZ", font_footer, tracking=3 * S)
    _tracked_text(draw, (W - MARGIN - kz_w, H - 52 * S), "DOPX.KZ", font_footer, brand_accent + (255,), tracking=3 * S)
    draw.line([(MARGIN, H - 64 * S), (W - MARGIN, H - 64 * S)], fill=(255, 255, 255, 25), width=1 * S)

    # Уменьшаем до финального размера.
    img = img.resize(CARD_SIZE, Image.LANCZOS)
    buffer = BytesIO()
    img.save(buffer, format="PNG", optimize=True)
    buffer.seek(0)
    default_storage.save(relative_path, buffer)
    return relative_path


def build_streak_share_card(*, username: str, streak_type: str, streak_count: int) -> str:
    """Карточка серии для шеринга.

    :param streak_type: 'evaluation' (туров подряд) или 'prediction' (угаданных подряд).
    Фон — готовая картинка, если есть. Возвращает путь в MEDIA.
    """
    if streak_type == "evaluation":
        eyebrow, label_line1, label_line2 = "СЕРИЯ ОЦЕНОК", "туров подряд", "оценили матч"
        accent = (96, 165, 250)
    else:
        eyebrow, label_line1, label_line2 = "СЕРИЯ ПРОГНОЗОВ", "прогнозов подряд", "угадали исход"
        accent = (167, 139, 250)

    custom_bg = _load_streak_background(streak_type)
    # mtime фона и версия дизайна — в ключе кэша.
    bg_marker = f"custom-{custom_bg[1]}" if custom_bg else "flat"
    key = _cache_key(username, streak_type, str(streak_count), "v2", bg_marker)
    relative_path = f"share-cards/streak_{key}.png"
    if default_storage.exists(relative_path):
        return relative_path

    S = SS_SCALE
    W, H = _CARD_RENDER_SIZE
    MARGIN = 64 * S

    if custom_bg is not None:
        img = custom_bg[0]
    else:
        img = Image.new("RGB", _CARD_RENDER_SIZE, (10, 10, 10))

    # Затемнение краёв + подложка слева.
    img = _edge_vignette(img, inset=36 * S, strength=0.45)
    scrim = _legibility_scrim(_CARD_RENDER_SIZE, start_alpha=235, end_fraction=0.50)
    img = Image.alpha_composite(img.convert("RGBA"), scrim).convert("RGB")

    draw = ImageDraw.Draw(img, "RGBA")

    # Логотип + название слева сверху.
    font_brand = _badge_font("cond_bold", 26)
    logo_size = 40 * S
    logo = _load_brand_mark(logo_size)
    if logo is not None:
        img_rgba = img.convert("RGBA")
        img_rgba.alpha_composite(logo, (MARGIN, 52 * S))
        img = img_rgba.convert("RGB")
        draw = ImageDraw.Draw(img, "RGBA")
        brand_x = MARGIN + logo_size + 16 * S
    else:
        brand_x = MARGIN
    _tracked_text(draw, (brand_x, 62 * S), "DOPX", font_brand, (240, 238, 244, 255), tracking=6 * S)

    # @username справа сверху на полупрозрачной плашке.
    font_user = _badge_font("regular", 22)
    user_text = _fit_single_line(draw, f"@{username}", font_user, W * 0.40)
    uw = draw.textlength(user_text, font=font_user)
    pad_x, pad_y = 16 * S, 9 * S
    px1, py0 = W - MARGIN, 48 * S
    px0 = px1 - uw - pad_x * 2
    py1 = py0 + 22 * S + pad_y * 2
    draw.rounded_rectangle([px0, py0, px1, py1], radius=(py1 - py0) // 2, fill=(6, 5, 10, 140))
    draw.text((px0 + pad_x, py0 + pad_y - 2 * S), user_text, font=font_user, fill=(225, 223, 232, 255))

    # Заголовок-эйброу.
    font_eyebrow = _badge_font("cond_bold", 20)
    ey_y = 168 * S
    _shadow_tracked_text(draw, (MARGIN, ey_y), eyebrow, font_eyebrow, accent + (255,), tracking=4 * S)
    draw.line([(MARGIN + 2 * S, ey_y + 34 * S), (MARGIN + 94 * S, ey_y + 34 * S)], fill=accent + (255,), width=3 * S)

    # Число серии: кегль уменьшается с числом цифр, подпись под числом.
    digits = len(str(streak_count))
    number_size = {1: 240, 2: 240, 3: 200}.get(digits, 160)
    font_number = _badge_font("bold", number_size)
    number_text = str(streak_count)
    num_y = 222 * S
    _shadow_text(draw, (MARGIN, num_y), number_text, font_number, accent + (255,), shadow_alpha=190, offset=(0, 6 * S))
    num_bottom = draw.textbbox((MARGIN, num_y), number_text, font=font_number)[3]

    font_label = _badge_font("bold", 32)
    label_y = num_bottom + 22 * S
    _shadow_text(draw, (MARGIN, label_y), label_line1, font_label, (245, 244, 248, 255), shadow_alpha=150, offset=(0, 2 * S))
    _shadow_text(draw, (MARGIN, label_y + 42 * S), label_line2, font_label, (245, 244, 248, 255), shadow_alpha=150, offset=(0, 2 * S))

    # Футер.
    font_footer = _badge_font("cond", 17)
    _tracked_text(draw, (MARGIN, H - 52 * S), "ГОЛОС ТРИБУН ИЗМЕРЯЕМ", font_footer, (170, 168, 178, 255), tracking=3 * S)
    kz_w = _tracked_text_width(draw, "DOPX.KZ", font_footer, tracking=3 * S)
    _tracked_text(draw, (W - MARGIN - kz_w, H - 52 * S), "DOPX.KZ", font_footer, accent + (255,), tracking=3 * S)
    draw.line([(MARGIN, H - 64 * S), (W - MARGIN, H - 64 * S)], fill=(255, 255, 255, 25), width=1 * S)

    # Тонкая рамка по периметру.
    draw.rectangle([1 * S, 1 * S, W - 2 * S, H - 2 * S], outline=(255, 255, 255, 30), width=1 * S)

    # Уменьшаем до финального размера.
    img = img.resize(CARD_SIZE, Image.LANCZOS)
    buffer = BytesIO()
    img.save(buffer, format="PNG", optimize=True)
    buffer.seek(0)
    default_storage.save(relative_path, buffer)
    return relative_path


def build_round_squad_share_card(
    *, season_year: str, tour: int, player_of_round_name: str,
    player_of_round_score: float | None, dramatic_match_label: str,
) -> str:
    """Карточка «DOPX Лучшие тура». Вызывается из round_squad/services.py при закрытии тура.
    Возвращает путь в MEDIA.
    """
    score_label = f"{player_of_round_score:.1f}/10" if player_of_round_score is not None else "—"
    key = _cache_key(season_year, str(tour), player_of_round_name, score_label, dramatic_match_label)
    relative_path = f"share-cards/round_{key}.png"
    if default_storage.exists(relative_path):
        return relative_path

    S = SS_SCALE
    img = Image.new("RGB", _CARD_RENDER_SIZE, color="#0a0a0a")
    draw = ImageDraw.Draw(img)
    font_title = _font("bold", 34)
    font_tour = _font("bold", 50)
    font_label = _font("regular", 26)
    font_name = _font("bold", 50)
    font_score = _font("bold", 40)
    font_small = _font("regular", 22)

    draw.text((60 * S, 50 * S), f"Сезон {season_year}", font=font_title, fill="#a3a3a3")
    draw.text((60 * S, 105 * S), f"DOPX Лучшие {tour}-го тура", font=font_tour, fill="#ffffff")

    draw.text((60 * S, 240 * S), "Игрок тура", font=font_label, fill="#60a5fa")
    draw.text((60 * S, 275 * S), player_of_round_name, font=font_name, fill="#ffffff")
    draw.text((60 * S, 340 * S), score_label, font=font_score, fill="#60a5fa")

    if dramatic_match_label:
        draw.text((60 * S, 440 * S), "Самый драматичный матч тура", font=font_label, fill="#a78bfa")
        draw.text((60 * S, 475 * S), dramatic_match_label, font=font_name, fill="#ffffff")

    draw.text((60 * S, _CARD_RENDER_SIZE[1] - 50 * S), "Голос трибун измеряем — dopx.kz", font=font_small, fill="#737373")

    img = img.resize(CARD_SIZE, Image.LANCZOS)
    buffer = BytesIO()
    img.save(buffer, format="PNG", optimize=True)
    buffer.seek(0)
    default_storage.save(relative_path, buffer)
    return relative_path


def build_player_season_recap_card(
    *, player_name: str, team_name: str, season_label: str,
    matches_played: int, avg_performance: float | None, goals: int,
) -> str:
    """Карточка итогов сезона игрока. Возвращает путь в MEDIA."""
    performance_label = f"{avg_performance:.1f}/10" if avg_performance is not None else "нет данных"
    key = _cache_key(player_name, team_name, season_label, str(matches_played), performance_label, str(goals))
    relative_path = f"share-cards/season_recap_{key}.png"
    if default_storage.exists(relative_path):
        return relative_path

    S = SS_SCALE
    img = Image.new("RGB", _CARD_RENDER_SIZE, color="#0a0a0a")
    draw = ImageDraw.Draw(img)
    font_title = _font("bold", 46)
    font_name = _font("bold", 58)
    font_label = _font("regular", 24)
    font_stat = _font("bold", 64)

    draw.text((60 * S, 50 * S), f"Сезон {season_label} на DOPX", font=font_title, fill="#a78bfa")
    draw.text((60 * S, 120 * S), player_name, font=font_name, fill="#ffffff")
    draw.text((60 * S, 195 * S), team_name, font=font_label, fill="#a3a3a3")

    # Три колонки статистики.
    columns = [
        ("Матчей сыграно", str(matches_played)),
        ("Средний рейтинг", performance_label),
        ("Голов", str(goals)),
    ]
    col_width = (_CARD_RENDER_SIZE[0] - 120 * S) // 3
    for i, (label, value) in enumerate(columns):
        x = 60 * S + i * col_width
        draw.text((x, 320 * S), value, font=font_stat, fill="#60a5fa")
        draw.text((x, 400 * S), label, font=font_label, fill="#a3a3a3")

    draw.text((60 * S, _CARD_RENDER_SIZE[1] - 50 * S), "Голос трибун измеряем — dopx.kz", font=font_label, fill="#737373")

    img = img.resize(CARD_SIZE, Image.LANCZOS)
    buffer = BytesIO()
    img.save(buffer, format="PNG", optimize=True)
    buffer.seek(0)
    default_storage.save(relative_path, buffer)
    return relative_path


def build_badge_share_card(
    *, username: str, badge_code: str, badge_name: str, badge_description: str,
    rarity: str, is_secret: bool, awarded_at,
) -> str:
    """Портретная карточка достижения.

    Чем выше редкость (BADGE_RARITY_META), тем больше эффектов: свечение, зерно,
    луч, искры. Фон — готовая картинка по редкости, если есть, иначе процедурный
    кристалл. Ключ кэша включает пользователя и дату получения.

    :param awarded_at: только для подписи даты.
    Возвращает путь в MEDIA.
    """
    rarity = rarity if rarity in BADGE_RARITY_META else "bronze"
    meta = BADGE_RARITY_META[rarity]
    date_label = awarded_at.strftime("%d.%m.%Y") if awarded_at else ""

    custom_bg = _load_custom_badge_background(rarity)
    # mtime фона — в ключе кэша.
    bg_marker = f"custom-{custom_bg[1]}" if custom_bg else "procedural"
    # Версия дизайна в ключе кэша.
    key = _cache_key(username, badge_code, rarity, date_label, "v4", bg_marker)
    relative_path = f"share-cards/badge_{key}.png"
    if default_storage.exists(relative_path):
        return relative_path

    # Размеры кристалла из BADGE_RARITY_META умножаются на S при вызове _draw_gem.
    S = SS_SCALE
    W, H = _BADGE_RENDER_SIZE
    top, bot = meta["top"], meta["bot"]

    if custom_bg is not None:
        img = custom_bg[0]
    else:
        img = Image.new("RGB", _BADGE_RENDER_SIZE, meta["base_bg"])

        # Два угловых свечения.
        glow_layer = Image.new("RGBA", _BADGE_RENDER_SIZE, (0, 0, 0, 0))
        gd = ImageDraw.Draw(glow_layer)
        gd.ellipse([W * 0.62 - 260 * S, 60 * S - 260 * S, W * 0.62 + 260 * S, 60 * S + 260 * S], fill=top + (45,))
        gd.ellipse([120 * S - 260 * S, H - 140 * S - 260 * S, 120 * S + 260 * S, H - 140 * S + 260 * S], fill=bot + (40,))
        glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(140 * S))
        img = Image.alpha_composite(img.convert("RGBA"), glow_layer).convert("RGB")

        if meta["beam"]:
            beam_layer = Image.new("RGBA", _BADGE_RENDER_SIZE, (0, 0, 0, 0))
            bx = W * 0.72
            ImageDraw.Draw(beam_layer).polygon(
                [(bx - 70 * S, -250 * S), (bx + 90 * S, -250 * S), (bx + 560 * S, H + 250 * S), (bx + 400 * S, H + 250 * S)], fill=(255, 255, 255, 10),
            )
            beam_layer = beam_layer.filter(ImageFilter.GaussianBlur(80 * S))
            img = Image.alpha_composite(img.convert("RGBA"), beam_layer).convert("RGB")

        if meta["grain"]:
            # Зерно — только для высоких редкостей.
            noise = Image.effect_noise(_BADGE_RENDER_SIZE, 16).convert("L")
            noise_rgb = Image.merge("RGB", (noise, noise, noise))
            img = Image.blend(img, noise_rgb, alpha=0.025)

        # Цветная полоса по левому краю (есть у всех редкостей).
        img.paste(Image.new("RGB", (8 * S, H), top), (0, 0))

        gem_cy = 430 * S
        img = _draw_gem(
            img, cx=int(W * 0.70), cy_center=gem_cy, height=meta["gem_h"] * S, width=meta["gem_w"] * S,
            color_top=top, color_bot=bot, n_sides=meta["n_sides"],
            seed=sum(badge_code.encode()) % 97, glow_alpha=meta["glow_alpha"], glow_scale=meta["glow_scale"],
        )

        if meta["sparkles"]:
            draw_s = ImageDraw.Draw(img, "RGBA")
            positions = [(200 * S, 220 * S, 7 * S), (880 * S, 650 * S, 5 * S), (300 * S, 760 * S, 4 * S), (140 * S, 560 * S, 6 * S)]
            for i in range(meta["sparkles"]):
                sx, sy, sr = positions[i % len(positions)]
                _draw_sparkle(draw_s, sx, sy, sr, top, alpha=180)

    # Затемнение краёв и подложка под текст — до любого текста.
    img = _edge_vignette(img, inset=90 * S, strength=0.85)
    scrim = _legibility_scrim(_BADGE_RENDER_SIZE, start_alpha=225, end_fraction=0.60)
    img = Image.alpha_composite(img.convert("RGBA"), scrim).convert("RGB")

    draw = ImageDraw.Draw(img, "RGBA")

    font_brand = _badge_font("cond_bold", 30)
    _shadow_tracked_text(draw, (56 * S, 52 * S), "DOPX", font_brand, (240, 232, 215, 255), tracking=9 * S)

    # Пилюля редкости. Ромб рисуем полигоном — в шрифте нет символов.
    font_pill = _badge_font("cond_bold", 21)
    pill_label = BADGE_RARITY_LABELS.get(rarity, rarity.upper())
    pill_w = _tracked_text_width(draw, pill_label, font_pill, tracking=3 * S) + 56 * S
    pill_h = 44 * S
    px0, py0 = W - 56 * S - pill_w, 46 * S
    draw.rounded_rectangle([px0, py0, px0 + pill_w, py0 + pill_h], radius=pill_h // 2, outline=top + (220,), width=2 * S, fill=(10, 9, 14, 205))
    dcx, dcy = px0 + 24 * S, py0 + pill_h / 2
    draw.polygon([(dcx, dcy - 7 * S), (dcx + 6 * S, dcy), (dcx, dcy + 7 * S), (dcx - 6 * S, dcy)], fill=top + (255,))
    _tracked_text(draw, (px0 + 40 * S, py0 + 12 * S), pill_label, font_pill, (235, 230, 240), tracking=3 * S)

    font_eyebrow = _badge_font("cond_bold", 20)
    ey_y = 420 * S
    _shadow_tracked_text(draw, (56 * S, ey_y), "ДОСТИЖЕНИЕ ПОЛУЧЕНО", font_eyebrow, top + (255,), tracking=4 * S)
    draw.line([(58 * S, ey_y + 38 * S), (150 * S, ey_y + 38 * S)], fill=top + (255,), width=3 * S)

    font_title = _badge_font("bold", 54)
    title_max_w = int(W * 0.56)
    title_lines = _wrap_text(draw, badge_name, font_title, title_max_w, max_lines=2)
    ty = ey_y + 62 * S
    for line in title_lines:
        _shadow_text(draw, (56 * S, ty), line, font_title, (250, 248, 252, 255), shadow_alpha=190, offset=(0, 4 * S))
        ty += 64 * S

    font_desc = _badge_font("regular", 25)
    desc_lines = _wrap_text(draw, badge_description, font_desc, title_max_w, max_lines=2)
    ty += 12 * S
    for line in desc_lines:
        _shadow_text(draw, (56 * S, ty), line, font_desc, (198, 196, 206, 255), shadow_alpha=160, offset=(0, 2 * S))
        ty += 33 * S

    # Нижняя панель: логотип + @username + дата, справа цитата.
    panel_y0, panel_y1 = H - 250 * S, H - 120 * S
    draw.rounded_rectangle([56 * S, panel_y0, W - 56 * S, panel_y1], radius=22 * S, fill=(10, 9, 14, 205), outline=(255, 255, 255, 25), width=1 * S)
    mid_x = (56 * S + W - 56 * S) // 2
    draw.line([(mid_x, panel_y0 + 22 * S), (mid_x, panel_y1 - 22 * S)], fill=(255, 255, 255, 35), width=1 * S)

    # Логотип DOPX — на всех редкостях.
    mark_size = 52 * S
    mark = _load_brand_mark(mark_size)
    mark_x, mark_y = 56 * S + 24 * S, (panel_y0 + panel_y1) // 2 - mark_size // 2
    if mark is not None:
        img_rgba = img.convert("RGBA")
        img_rgba.alpha_composite(mark, (int(mark_x), int(mark_y)))
        img = img_rgba.convert("RGB")
        draw = ImageDraw.Draw(img, "RGBA")
        ux = mark_x + mark_size + 20 * S
    elif meta["wreath"]:
        # Fallback — венок, если логотипа нет.
        img = _draw_laurel(img, 56 * S + 58 * S, (panel_y0 + panel_y1) // 2 - 6 * S, 42 * S, top)
        draw = ImageDraw.Draw(img, "RGBA")
        ux = 56 * S + 140 * S
    else:
        ux = 56 * S + 24 * S

    font_user = _badge_font("bold", 24)
    font_date = _badge_font("regular", 18)
    draw.text((ux, (panel_y0 + panel_y1) // 2 - 25 * S), f"@{username}", font=font_user, fill=(240, 238, 244))
    draw.text((ux, (panel_y0 + panel_y1) // 2 + 5 * S), f"получено {date_label}" if date_label else "", font=font_date, fill=(160, 158, 168))

    font_quote_mark = _badge_font("bold", 38)
    qx = mid_x + 30 * S
    draw.text((qx, panel_y0 + 18 * S), "“", font=font_quote_mark, fill=top + (200,))
    font_quote = _badge_font("italic", 20)
    quote_lines = _wrap_text(draw, BADGE_RARITY_QUOTES.get(rarity, ""), font_quote, (W - 56 * S - 24 * S) - qx - 32 * S, max_lines=3)
    qy = panel_y0 + 50 * S
    for line in quote_lines:
        draw.text((qx + 30 * S, qy), line, font=font_quote, fill=(210, 208, 220))
        qy += 27 * S

    font_footer = _badge_font("cond", 18)
    _tracked_text(draw, (56 * S, H - 56 * S), "ГОЛОС ТРИБУН ИЗМЕРЯЕМ", font_footer, (120, 118, 128), tracking=3 * S)
    kz_w = _tracked_text_width(draw, "DOPX.KZ", font_footer, tracking=3 * S)
    _tracked_text(draw, (W - 56 * S - kz_w, H - 56 * S), "DOPX.KZ", font_footer, top, tracking=3 * S)
    draw.line([(320 * S, H - 46 * S), (W - 56 * S - kz_w - 24 * S, H - 46 * S)], fill=(255, 255, 255, 25), width=1 * S)

    # Тонкая внутренняя рамка.
    draw.rounded_rectangle([2 * S, 2 * S, W - 3 * S, H - 3 * S], radius=34 * S, outline=(255, 255, 255, 25), width=1 * S)

    # Скруглённые прозрачные углы (карточка не используется как og:image).
    mask = _rounded_alpha_mask(_BADGE_RENDER_SIZE, 36 * S)
    out = Image.new("RGBA", _BADGE_RENDER_SIZE, (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)

    # Уменьшаем до финального размера (RGBA).
    out = out.resize(BADGE_CARD_SIZE, Image.LANCZOS)
    buffer = BytesIO()
    out.save(buffer, format="PNG", optimize=True)
    buffer.seek(0)
    default_storage.save(relative_path, buffer)
    return relative_path
