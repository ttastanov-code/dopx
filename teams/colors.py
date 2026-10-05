# teams/colors.py
"""Фирменный цвет клуба из герба: самый частый насыщенный цвет, без белого, чёрного и серого.
Нужен для подсветки шапки матча в цветах команд."""
from __future__ import annotations

import colorsys
import io
import logging
from collections import Counter

import requests

logger = logging.getLogger(__name__)
FALLBACK = "#6366f1"


def dominant_color(image_bytes: bytes) -> str:
    """#rrggbb — самый частый «живой» цвет; у монохромного герба — запасной."""
    from PIL import Image

    img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    img.thumbnail((64, 64))
    counts: Counter = Counter()
    for r, g, b, a in img.getdata():
        if a < 200:
            continue
        h, l, s = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
        if s < 0.35 or l < 0.12 or l > 0.88:
            continue  # белый, чёрный, серый — не фирменный цвет
        counts[(r // 16 * 16 + 8, g // 16 * 16 + 8, b // 16 * 16 + 8)] += 1
    if not counts:
        return FALLBACK
    r, g, b = counts.most_common(1)[0][0]
    return f"#{r:02x}{g:02x}{b:02x}"


def refresh_brand_color(team, force: bool = False) -> str:
    """Посчитать и сохранить цвет клуба (герб: загруженный файл или ссылка поставщика)."""
    if team.brand_color and not force:
        return team.brand_color
    try:
        if team.logo:
            data = team.logo.read()
        elif team.logo_url:
            data = requests.get(team.logo_url, timeout=10).content
        else:
            return ""
        color = dominant_color(data)
    except Exception:
        logger.warning("Цвет клуба %s не посчитан", team, exc_info=True)
        return ""
    type(team).objects.filter(pk=team.pk).update(brand_color=color)
    team.brand_color = color
    return color
