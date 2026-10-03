# core/services/story_cards.py
"""Вертикальные карточки 1080×1920 для Instagram Stories: «большое число + подпись» в стиле DOPX.
Кэш — по содержимому, в MEDIA/share-cards/."""
from __future__ import annotations

from io import BytesIO
from pathlib import Path

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .share_cards import _cache_key, _wrap_text

SIZE = (1080, 1920)
FONTS = Path(settings.BASE_DIR) / "static" / "fonts"
LOGO = Path(settings.BASE_DIR) / "static" / "img" / "dopx-logo-icon.png"
ACCENTS = {"fan_top": (52, 211, 153), "season": (251, 191, 36), "day_streak": (251, 146, 60),
           "predictions": (96, 165, 250), "match": (167, 139, 250), "prediction_hit": (96, 165, 250)}


def _vertical_gradient(top: tuple, bottom: tuple) -> Image.Image:
    """Ровный вертикальный градиент (у повёрнутого из share_cards на 9:16 видны углы)."""
    mask = Image.linear_gradient("L").resize(SIZE)
    return Image.composite(Image.new("RGB", SIZE, bottom), Image.new("RGB", SIZE, top), mask)


def _font(bold: bool, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(str(FONTS / ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf")), size)
    except OSError:
        return ImageFont.load_default(size=size)


def _center(draw: ImageDraw.ImageDraw, y: int, text: str, font, fill) -> int:
    w = draw.textlength(text, font=font)
    draw.text(((SIZE[0] - w) / 2, y), text, font=font, fill=fill)
    return y + font.size + int(font.size * 0.3)


def _fit(draw, text: str, bold: bool, start: int, max_width: int, min_size: int = 60):
    """Самый крупный кегль, при котором строка влезает по ширине."""
    size = start
    while size > min_size and draw.textlength(text, font=_font(bold, size)) > max_width:
        size -= 8
    return _font(bold, size)


def build_story_card(*, kind: str, eyebrow: str, number_text: str, label_line1: str, label_line2: str = "",
                     footer_note: str = "") -> str:
    """Путь PNG в хранилище. eyebrow — надпись сверху, number_text — главное число, label — подпись."""
    key = _cache_key("story", kind, eyebrow, number_text, label_line1, label_line2, footer_note, "v2")
    path = f"share-cards/story_{key}.png"
    if default_storage.exists(path):
        return path
    accent = ACCENTS.get(kind, (167, 139, 250))
    img = _vertical_gradient((11, 11, 20), (26, 16, 51)).convert("RGBA")
    # Мягкое свечение акцентного цвета за числом.
    glow = Image.new("RGBA", SIZE, (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse((90, 520, 990, 1420), fill=accent + (90,))
    img = Image.alpha_composite(img, glow.filter(ImageFilter.GaussianBlur(180)))
    draw = ImageDraw.Draw(img)
    margin = 90

    if LOGO.exists():
        logo = Image.open(LOGO).convert("RGBA")
        logo.thumbnail((120, 120))
        img.alpha_composite(logo, ((SIZE[0] - logo.width) // 2, 150))
    _center(draw, 300, "DOPX", _font(True, 64), (255, 255, 255))
    _center(draw, 380, "голос трибун", _font(False, 36), (255, 255, 255, 150))

    y = 640
    for line in _wrap_text(draw, eyebrow.upper(), _font(True, 40), SIZE[0] - 2 * margin, 2):
        y = _center(draw, y, line, _font(True, 40), accent)
    y += 30
    num_font = _fit(draw, number_text, True, 300, SIZE[0] - 2 * margin)
    y = _center(draw, y, number_text, num_font, (255, 255, 255))
    y += 20
    for line in _wrap_text(draw, label_line1, _font(True, 64), SIZE[0] - 2 * margin, 3):
        y = _center(draw, y, line, _font(True, 64), (255, 255, 255))
    if label_line2:
        y += 10
        for line in _wrap_text(draw, label_line2, _font(False, 46), SIZE[0] - 2 * margin, 2):
            y = _center(draw, y, line, _font(False, 46), (255, 255, 255, 170))

    if footer_note:
        _center(draw, 1600, footer_note, _font(False, 38), (255, 255, 255, 160))
    draw.rounded_rectangle((300, 1690, 780, 1780), radius=45, fill=accent + (255,))
    _center(draw, 1712, "dopx.kz", _font(True, 44), (11, 11, 20))

    out = BytesIO()
    img.convert("RGB").save(out, "PNG", optimize=True)
    return default_storage.save(path, ContentFile(out.getvalue()))
