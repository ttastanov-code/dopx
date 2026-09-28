"""Фирменные ассеты DOPX из одного описания знака «Мяч-шкала».

    app_venv/bin/python scripts/build_brand_assets.py

Пишет SVG (static/img) и PNG (static/img, static/favicons, static/pwa). Правки знака — только здесь,
потом запустить скрипт заново. PNG рисуются Pillow с запасом разрешения и сглаживанием при уменьшении.
"""
from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
GRAD_FROM, GRAD_TO = (0x4F, 0x6B, 0xFF), (0x93, 0x33, 0xEA)
INK = (0x11, 0x18, 0x27)
def _ss(size: int) -> int:
    """Запас разрешения при рисовании: мелким иконкам больше, крупным меньше (память)."""
    return max(2, min(8, 2048 // size))

# Геометрия знака в сетке 48×48: шкала от 135° по часовой через верх до 320°, бегунок на конце.
C, R = 24.0, 17.0
TRACK_W, ARC_W, KNOB_R, KNOB_W = 3.0, 5.0, 5.2, 3.2
ARC_FROM, ARC_TO = 135, 320
PENTAGON = [(24, 17.5), (30.2, 22), (27.8, 29.3), (20.2, 29.3), (17.8, 22)]
TILE_RADIUS, TILE_INSET = 13, 0.8  # знак в плитке занимает 80%


def _pt(deg: float, r: float = R) -> tuple[float, float]:
    a = math.radians(deg)
    return C + r * math.cos(a), C + r * math.sin(a)


KNOB = _pt(ARC_TO)


# ---------------------------------------------------------------------------- SVG

def svg_mark(paint: str, hole: str, track_opacity: str = ".22") -> str:
    """Знак без фона: paint — цвет, hole — заливка «дырки» бегунка (цвет фона)."""
    sx, sy = _pt(ARC_FROM)
    pts = " ".join(f"L{x} {y}" if i else f"M{x} {y}" for i, (x, y) in enumerate(PENTAGON)) + " Z"
    return (
        f'<circle cx="{C:g}" cy="{C:g}" r="{R:g}" fill="none" stroke="{paint}" stroke-width="{TRACK_W:g}" opacity="{track_opacity}"/>'
        f'<path d="M{sx:.2f} {sy:.2f} A{R:g} {R:g} 0 1 1 {KNOB[0]:.2f} {KNOB[1]:.2f}" fill="none" stroke="{paint}" '
        f'stroke-width="{ARC_W:g}" stroke-linecap="round"/>'
        f'<circle cx="{KNOB[0]:.2f}" cy="{KNOB[1]:.2f}" r="{KNOB_R:g}" fill="{hole}" stroke="{paint}" stroke-width="{KNOB_W:g}"/>'
        f'<path d="{pts}" fill="{paint}"/>'
    )


def _gradient(gid: str, size: float) -> str:
    return (f'<linearGradient id="{gid}" x1="0" y1="0" x2="{size:g}" y2="{size:g}" gradientUnits="userSpaceOnUse">'
            f'<stop offset="0" stop-color="#4F6BFF"/><stop offset="1" stop-color="#9333EA"/></linearGradient>')


def svg_tile_body(gid: str) -> str:
    offset = 48 * (1 - TILE_INSET) / 2
    return (f'<defs>{_gradient(gid, 48)}</defs><rect width="48" height="48" rx="{TILE_RADIUS}" fill="url(#{gid})"/>'
            f'<g transform="translate({offset:g} {offset:g}) scale({TILE_INSET:g})">'
            f'{svg_mark("#fff", f"url(#{gid})", ".35")}</g>')


# ---------------------------------------------------------------------------- PNG

def _gradient_image(size: int) -> Image.Image:
    """Диагональный градиент: считаем 64×64 и растягиваем — для линейного перехода это без потерь."""
    small = 64
    img = Image.new("RGB", (small, small))
    px = img.load()
    for y in range(small):
        for x in range(small):
            t = (x + y) / (2 * (small - 1))
            px[x, y] = tuple(round(a + (b - a) * t) for a, b in zip(GRAD_FROM, GRAD_TO))
    return img.resize((size, size), Image.BILINEAR)


def _draw_mark(layer: Image.Image, scale: float, ox: float, oy: float, color, hole_color=None):
    """Знак в слой RGBA: координаты сетки 48 × scale со сдвигом (ox, oy)."""
    def p(x, y):
        return ox + x * scale, oy + y * scale

    d = ImageDraw.Draw(layer)
    track = Image.new("RGBA", layer.size, (0, 0, 0, 0))
    td = ImageDraw.Draw(track)
    tw = TRACK_W * scale
    cx, cy = p(C, C)
    r = R * scale
    td.ellipse([cx - r - tw / 2, cy - r - tw / 2, cx + r + tw / 2, cy + r + tw / 2], outline=color + (255,), width=round(tw))
    alpha = track.getchannel("A").point(lambda v: round(v * 0.35))
    track.putalpha(alpha)
    layer.alpha_composite(track)

    aw = ARC_W * scale
    d.arc([cx - r - aw / 2, cy - r - aw / 2, cx + r + aw / 2, cy + r + aw / 2], ARC_FROM, ARC_TO, fill=color + (255,), width=round(aw))
    for deg in (ARC_FROM, ARC_TO):  # круглые концы шкалы
        ex, ey = p(*_pt(deg))
        d.ellipse([ex - aw / 2, ey - aw / 2, ex + aw / 2, ey + aw / 2], fill=color + (255,))

    kx, ky = p(*KNOB)
    kr, kw = KNOB_R * scale, KNOB_W * scale
    d.ellipse([kx - kr - kw / 2, ky - kr - kw / 2, kx + kr + kw / 2, ky + kr + kw / 2], fill=color + (255,))
    inner = kr - kw / 2
    hole = hole_color + (255,) if hole_color else (0, 0, 0, 0)
    if hole_color:
        d.ellipse([kx - inner, ky - inner, kx + inner, ky + inner], fill=hole)
    else:  # прозрачная «дырка»: вычитаем из альфы
        mask = Image.new("L", layer.size, 0)
        ImageDraw.Draw(mask).ellipse([kx - inner, ky - inner, kx + inner, ky + inner], fill=255)
        a = layer.getchannel("A")
        a.paste(0, mask=mask)
        layer.putalpha(a)
    d.polygon([p(x, y) for x, y in PENTAGON], fill=color + (255,))


def png_tile(size: int) -> Image.Image:
    big = size * _ss(size)
    grad = _gradient_image(big).convert("RGBA")
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, big - 1, big - 1], radius=big * TILE_RADIUS / 48, fill=255)
    tile = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    tile.paste(grad, mask=mask)
    scale = big / 48 * TILE_INSET
    offset = big * (1 - TILE_INSET) / 2
    kx, ky = KNOB[0] * scale + offset, KNOB[1] * scale + offset
    t = (kx + ky) / (2 * big)
    hole = tuple(round(a + (b - a) * t) for a, b in zip(GRAD_FROM, GRAD_TO))
    _draw_mark(tile, scale, offset, offset, (255, 255, 255), hole)
    return tile.resize((size, size), Image.LANCZOS)


def png_mono(size: int, color=(255, 255, 255)) -> Image.Image:
    """Знак одним цветом на прозрачном фоне (значок push-уведомлений)."""
    big = size * _ss(size)
    layer = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    _draw_mark(layer, big / 48, 0, 0, color)
    return layer.resize((size, size), Image.LANCZOS)


def png_logo(width: int, height: int) -> Image.Image:
    """Плитка + надпись DOPX (письма, og:image по умолчанию)."""
    big_w, big_h = width * 2, height * 2
    img = Image.new("RGBA", (big_w, big_h), (0, 0, 0, 0))
    icon = round(big_h * 0.82)
    pad = (big_h - icon) // 2
    img.alpha_composite(png_tile(icon), (pad, pad))
    font = ImageFont.truetype(str(STATIC / "fonts" / "LiberationSans-Bold.ttf"), round(icon * 0.62))
    d = ImageDraw.Draw(img)
    box = d.textbbox((0, 0), "DOPX", font=font)
    x = pad + icon + round(icon * 0.22)
    y = (big_h - (box[3] - box[1])) // 2 - box[1]
    d.text((x, y), "DOPX", font=font, fill=INK + (255,))
    return img.resize((width, height), Image.LANCZOS)


def main():
    (STATIC / "img").mkdir(exist_ok=True)
    tile_svg = f'<svg width="512" height="512" viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg">{svg_tile_body("dopxGradient")}</svg>\n'
    (STATIC / "img" / "dopx-logo-icon.svg").write_text(tile_svg)
    mark_svg = (f'<svg width="48" height="48" viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg">'
                f'{svg_mark("currentColor", "none")}</svg>\n')
    (STATIC / "img" / "dopx-mark.svg").write_text(mark_svg)
    logo_svg = (f'<svg width="640" height="160" viewBox="0 0 140 32" xmlns="http://www.w3.org/2000/svg">'
                f'<svg width="32" height="32" viewBox="0 0 48 48">{svg_tile_body("dopxGradientLogo")}</svg>'
                f'<text x="40" y="23.5" font-family="Inter, \'Liberation Sans\', Arial, sans-serif" font-weight="800" '
                f'font-size="19" letter-spacing="-0.3" fill="#111827">DOPX</text></svg>\n')
    (STATIC / "img" / "dopx-logo.svg").write_text(logo_svg)

    png_tile(1024).save(STATIC / "img" / "dopx-logo-icon.png")
    png_logo(1914, 680).save(STATIC / "img" / "dopx-logo.png")
    png_tile(16).save(STATIC / "favicons" / "favicon-16x16.png")
    png_tile(32).save(STATIC / "favicons" / "favicon-32x32.png")
    # apple-touch и PWA без прозрачных углов: система сама скругляет иконку.
    for size, path in ((180, STATIC / "favicons" / "apple-touch-icon.png"),
                       (192, STATIC / "pwa" / "icon-192.png"), (512, STATIC / "pwa" / "icon-512.png")):
        big = size * _ss(size)
        full = _gradient_image(big).convert("RGBA")
        scale = big / 48 * 0.64  # запас под маску maskable-иконок
        offset = big * (1 - 0.64) / 2
        kx, ky = KNOB[0] * scale + offset, KNOB[1] * scale + offset
        t = (kx + ky) / (2 * big)
        hole = tuple(round(a + (b - a) * t) for a, b in zip(GRAD_FROM, GRAD_TO))
        _draw_mark(full, scale, offset, offset, (255, 255, 255), hole)
        full.resize((size, size), Image.LANCZOS).save(path)
    png_mono(96).save(STATIC / "pwa" / "badge-96.png")
    print("Готово: static/img/dopx-logo*.{svg,png}, dopx-mark.svg, favicons, pwa.")


if __name__ == "__main__":
    main()
