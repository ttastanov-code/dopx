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
from io import BytesIO
from pathlib import Path

from django.conf import settings
from django.core.files.storage import default_storage
from PIL import Image, ImageDraw, ImageFont

CARD_SIZE = (1200, 630)  # стандартный OG-image размер
FONTS_DIR = Path(settings.BASE_DIR) / "static" / "fonts"


def _font(name: str, size: int) -> ImageFont.FreeTypeFont:
    """
    :param name: 'bold' или 'regular'.

    Не падает, если TTF-файла нет на диске (например, в свежем клоне
    репозитория до первого `collectstatic`) — молча откатывается на
    growing default bitmap-шрифт Pillow, чтобы генерация карточки не
    роняла запрос целиком из-за отсутствующего файла шрифта.
    """
    filename = "LiberationSans-Bold.ttf" if name == "bold" else "LiberationSans-Regular.ttf"
    try:
        return ImageFont.truetype(str(FONTS_DIR / filename), size)
    except OSError:
        return ImageFont.load_default(size=size)


def _cache_key(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:24]


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
