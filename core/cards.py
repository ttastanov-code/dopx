# core/cards.py
"""Генерируемые картинки (шеринг, соцсети, сторис, достижения): SVG-шаблоны templates/cards/ → PNG через resvg.
Перенос строк и размер текста считаем здесь по тем же TTF, логотипы клубов и фото игроков скачиваем один раз и кэшируем.
Готовый PNG кэшируется в MEDIA по хэшу содержимого."""
from __future__ import annotations

import base64
import hashlib
import logging
from functools import lru_cache
from io import BytesIO
from pathlib import Path

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.template.loader import render_to_string

logger = logging.getLogger(__name__)

FONT_DIR = Path(settings.BASE_DIR) / "static" / "fonts" / "cards"
FONTS = {"display": "InterDisplay-ExtraBold.ttf", "display700": "InterDisplay-Bold.ttf",
         "bold": "Inter-ExtraBold.ttf", "semibold": "Inter-SemiBold.ttf", "regular": "Inter-Regular.ttf"}
DESIGN_VERSION = "v3"

# Акцент карточки: основной цвет и второй для свечения.
ACCENTS = {
    "violet": ("#A78BFA", "#4F6BFF"), "green": ("#34D399", "#0EA5E9"), "amber": ("#FBBF24", "#F97316"),
    "orange": ("#FB923C", "#EF4444"), "blue": ("#60A5FA", "#8B5CF6"), "red": ("#F87171", "#A855F7"),
    "pink": ("#F472B6", "#8B5CF6"),
}


@lru_cache(maxsize=64)
def _pil_font(kind: str, size: int):
    from PIL import ImageFont
    return ImageFont.truetype(str(FONT_DIR / FONTS[kind]), size)


def measure(text: str, size: int, kind: str = "bold", tracking: float = 0) -> float:
    return _pil_font(kind, size).getlength(text) + tracking * size * max(len(text) - 1, 0)


def fit(text: str, kind: str, start: int, max_width: float, min_size: int = 20) -> int:
    """Самый крупный кегль, при котором строка влезает по ширине."""
    size = start
    while size > min_size and measure(text, size, kind) > max_width:
        size -= 2
    return size


def wrap(text: str, size: int, max_width: float, kind: str = "bold", max_lines: int = 2) -> list[str]:
    """Перенос по словам; не влезло — последняя строка с многоточием."""
    words, lines, line = (text or "").split(), [], ""
    for word in words:
        trial = f"{line} {word}".strip()
        if measure(trial, size, kind) <= max_width or not line:
            line = trial
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while last and measure(last + "…", size, kind) > max_width:
            last = last[:-1]
        lines[-1] = last.rstrip() + "…"
    return lines


def image_data(url: str, size: int = 320) -> str:
    """Картинка (логотип клуба, фото игрока) как data URI для SVG. Скачивается один раз; не вышло — пусто."""
    if not url:
        return ""
    path = f"card-assets/{hashlib.sha1(url.encode()).hexdigest()}.png"
    try:
        if not default_storage.exists(path):
            from PIL import Image

            if url.startswith(settings.MEDIA_URL):  # загружено на сайт — читаем из хранилища
                with default_storage.open(url[len(settings.MEDIA_URL):], "rb") as fh:
                    raw = fh.read()
            else:
                import requests
                resp = requests.get(url, timeout=6)
                resp.raise_for_status()
                raw = resp.content
            img = Image.open(BytesIO(raw)).convert("RGBA")
            img.thumbnail((size, size))
            out = BytesIO()
            img.save(out, "PNG", optimize=True)
            default_storage.save(path, ContentFile(out.getvalue()))
        with default_storage.open(path, "rb") as fh:
            return "data:image/png;base64," + base64.b64encode(fh.read()).decode()
    except Exception as e:  # сеть, битый файл — карточка без картинки, с инициалами
        logger.warning("cards: картинка %s не загрузилась: %s", url, e)
        return ""


def team_logo(team) -> str:
    return (getattr(team, "logo_display", "") or "") if team else ""


def player_photo(player) -> str:
    return (getattr(player, "photo_display", "") or "") if player else ""


def initials(name: str) -> str:
    parts = [p for p in (name or "").replace("-", " ").split() if p[:1].isalpha()]
    return ("".join(p[0] for p in parts[:2]) or "?").upper()


@lru_cache(maxsize=1)
def _templates_hash() -> str:
    """Отпечаток всех шаблонов карточек: правка дизайна — новые картинки, старый кэш не отдаётся."""
    root = Path(settings.BASE_DIR) / "templates" / "cards"
    digest = hashlib.sha1()
    for f in sorted(root.glob("*.svg")):
        digest.update(f.read_bytes())
    return digest.hexdigest()[:10]


def render(template: str, context: dict, *, prefix: str, key_parts: tuple, size: tuple[int, int]) -> str:
    """SVG-шаблон → PNG в MEDIA; повторный вызов с теми же данными отдаёт готовый файл."""
    import resvg_py

    key = hashlib.sha256("|".join(map(str, (DESIGN_VERSION, _templates_hash(), template, *key_parts))).encode()).hexdigest()[:24]
    path = f"share-cards/{prefix}_{key}.png"
    if default_storage.exists(path):
        return path
    accent, accent2 = ACCENTS.get(context.get("accent_name", "violet"), ACCENTS["violet"])
    svg = render_to_string(template, {"w": size[0], "h": size[1], "accent": accent, "accent2": accent2, **context})
    png = resvg_py.svg_to_bytes(svg_string=svg, font_dirs=[str(FONT_DIR)], skip_system_fonts=True,
                                font_family="Inter", width=size[0], height=size[1])
    return default_storage.save(path, ContentFile(bytes(png)))


# ---------------- Общие куски OG-карточек 1200×630
OG = (1200, 630)


def sentence(text: str) -> str:
    """Рубрики приходят капсом — показываем в обычном регистре, бренд и 1X2 не трогаем."""
    if not text.isupper():
        return text
    out = text[:1] + text[1:].lower()
    for word in ("DOPX", "1X2", "КПЛ", "ДНК"):
        out = out.replace(word.lower(), word).replace(word.capitalize(), word)
    return out


def _chip(text: str, right: int = 1136) -> dict:
    """Рубрика в стеклянной капсуле: точка-акцент и текст в обычном регистре."""
    text = sentence(text)
    width = int(measure(text, 19, "semibold")) + 62
    x = right - width
    return {"chip": text, "chip_w": width, "chip_x": x, "chip_dot": x + 24, "chip_text_x": x + 40}


def _footer(note: str, h: int = 630, w: int = 1200) -> dict:
    return {"footer_note": note, "footer_text_y": h - 52, "footer_x2": w - 64}


def stat_card(*, prefix: str, accent_name: str, chip: str, number: str, label: str, sub: str = "",
              footer_note: str = "", image_url: str = "", progress: float = 0.72, key_extra: tuple = ()) -> str:
    """«Большое число + подпись»: серии, похвастаться, соцсети. image_url — фото игрока или логотип клуба в кольце."""
    number_size = fit(number, "display", 210, 600, 80)
    label_lines = wrap(label, 42, 580, "display700", 2)
    sub_lines = wrap(sub, 25, 580, "semibold", 2) if sub else []
    top = 150 + int(number_size * 0.86)
    labels = [{"text": t, "y": top + 66 + i * 50} for i, t in enumerate(label_lines)]
    y = (labels[-1]["y"] if labels else top) + 42
    subs = [{"text": t, "y": y + i * 34} for i, t in enumerate(sub_lines)]
    image = image_data(image_url)
    ctx = {"accent_name": accent_name, "number": number, "number_size": number_size, "number_y": top,
           "label_lines": labels, "sub_lines": subs, "image": image, "image_fit": "slice" if "players" in image_url else "meet",
           "image_size": 224 if "players" in image_url else 150, "image_off": -112 if "players" in image_url else -75,
           "number_tracking": f"{-number_size * 0.045:.1f}",
           "arc": int(943 * max(0.05, min(progress, 1.0))), **_chip(chip), **_footer(footer_note or "Голос трибун · оценки болельщиков")}
    return render("cards/og_stat.svg", ctx, prefix=prefix, size=OG,
                  key_parts=(accent_name, chip, number, label, sub, footer_note, image_url, progress, *key_extra))


def _team(name: str, logo_url: str, cx: int, cy: int, r: int, name_size: int = 28) -> dict:
    lines = wrap(name, name_size, 300, "display700", 2)
    return {"cx": cx, "cy": cy, "r": r, "img": int(r * 0.62), "img2": int(r * 1.24), "logo": image_data(logo_url),
            "initials": initials(name), "ini_size": int(r * 0.7), "ini_y": int(r * 0.25), "name_size": name_size,
            "name_lines": [{"text": t, "y": r + 46 + i * (name_size + 6)} for i, t in enumerate(lines)]}


def match_card(*, home: str, away: str, score: str, home_logo: str = "", away_logo: str = "",
               hero_name: str = "", hero_score: str = "", hero_photo: str = "") -> str:
    """Превью матча: логотипы, счёт, лучший игрок по оценкам."""
    hero = None
    if hero_name:
        photo = image_data(hero_photo)
        hero = {"name": wrap(hero_name, 27, 350 if photo else 410, "display700", 1)[0], "score": hero_score,
                "photo": photo, "text_x": 390 if photo else 330}
    ctx = {"accent_name": "violet", "home": _team(home, home_logo, 210, 262, 90), "away": _team(away, away_logo, 990, 262, 90),
           "score": score, "score_size": fit(score, "display", 170, 400, 80), "score_y": 322, "hero": hero,
           **_chip("ИТОГ МАТЧА"), **_footer("Оценки игроков от болельщиков")}
    return render("cards/og_match.svg", ctx, prefix="match", size=OG,
                  key_parts=(home, away, score, home_logo, away_logo, hero_name, hero_score, hero_photo))


def dna_card(*, home: str, away: str, score: str, headline: str, drama_level: str, drama_index: float,
             hero: tuple = ("", ""), antihero: tuple = ("", ""), mood: str = "", consensus: str = "",
             home_logo: str = "", away_logo: str = "") -> str:
    """ДНК матча: главный факт, шкала драмы, герой и антигерой, настроение трибун."""
    accent = {"high": "orange", "medium": "amber", "low": "blue"}.get(drama_level, "violet")
    label = {"high": "Высокая драма", "medium": "Средняя драма", "low": "Спокойный матч"}.get(drama_level, "Драма")
    score_line = f"{home}  {score}  {away}"
    hl_size = fit(headline, "display", 46, 1280, 30) if headline else 40
    hl_lines = wrap(headline, hl_size, 636, "display", 2) if headline else []
    side = [t for t in (mood, consensus) if t]
    people = []
    for i, (name, value, title, color) in enumerate(((*hero, "Герой матча", "#34D399"), (*antihero, "Антигерой", "#F87171"))):
        if not name:
            continue
        y = 150 + len(people) * 168
        people.append({"y": y, "y_dot": y + 34, "y_label": y + 40, "y_score": y + 110, "label": title, "color": color, "score": value,
                       "name_lines": [{"text": t, "y": y + 80 + j * 30} for j, t in enumerate(wrap(name, 25, 220, "display700", 2))]})
    ctx = {"accent_name": accent, "home": {"logo": image_data(home_logo)}, "away": {"logo": image_data(away_logo)},
           "score_line": score_line, "headline_size": hl_size,
           "headline_lines": [{"text": t, "y": 252 + i * (hl_size + 10)} for i, t in enumerate(hl_lines)],
           "drama_label": label, "drama_value": f"{round(drama_index)}/100", "drama_w": int(636 * max(0, min(drama_index, 100)) / 100),
           "side": [{"text": wrap(t, 21, 640, "semibold", 1)[0], "y": 440 + i * 32} for i, t in enumerate(side[:2])],
           "people": people, **_chip("ДНК МАТЧА"), **_footer("Как трибуны увидели матч")}
    ctx["score_x"] = 122 if ctx["home"]["logo"] else 64
    ctx["away_logo_x"] = ctx["score_x"] + int(measure(score_line, 28, "display700")) + 14
    return render("cards/og_dna.svg", ctx, prefix="dna", size=OG,
                  key_parts=(home, away, score, headline, drama_level, drama_index, hero, antihero, mood, consensus, home_logo, away_logo))


def round_card(*, season: str, tour: int, player: str, score: str, drama: str = "", photo_url: str = "") -> str:
    """Лучшие тура: игрок тура, оценка, самый драматичный матч."""
    size = fit(player, "display", 76, 1150, 44)
    lines = wrap(player, size, 640, "display", 2)
    names = [{"text": t, "y": 196 + int(size * 1.02) + i * (size + 6)} for i, t in enumerate(lines)]
    badge_y = names[-1]["y"] + 34
    ctx = {"accent_name": "green", "name_lines": names, "name_size": size, "score": score, "badge_y": badge_y,
           "badge_text_y": badge_y + 41, "drama": wrap(drama, 25, 560, "display700", 1)[0] if drama else "", "tour": tour,
           "photo": image_data(photo_url), **_chip(f"ЛУЧШИЕ ТУРА · {season}"), **_footer("Сборная тура по оценкам болельщиков")}
    return render("cards/og_round.svg", ctx, prefix="round", size=OG, key_parts=(season, tour, player, score, drama, photo_url))


def recap_card(*, player: str, team: str, season: str, matches: int, rating: str, goals: int,
               photo_url: str = "", team_logo: str = "") -> str:
    """Итоги сезона игрока: фото, клуб, три цифры."""
    size = fit(player, "display", 62, 1300, 36)
    lines = wrap(player, size, 700, "display", 2)
    names = [{"text": t, "y": 196 + int(size * 1.0) + i * (size + 4)} for i, t in enumerate(lines)]
    stats = [(str(matches), "матчей"), (rating, "средняя оценка"), (str(goals), "голов")]
    ctx = {"accent_name": "blue", "name_lines": names, "name_size": size, "team": team, "team_y": names[-1]["y"] + 42,
           "season": season.upper(), "photo": image_data(photo_url), "team_logo": image_data(team_logo), "initials": initials(player),
           "stats": [{"x": 430 + i * 236, "cx": 538 + i * 236, "value": v, "label": l, "size": fit(v, "display", 56, 180, 26)}
                     for i, (v, l) in enumerate(stats)],
           **_chip("СЕЗОН ИГРОКА"), **_footer("Средняя оценка болельщиков DOPX")}
    return render("cards/og_recap.svg", ctx, prefix="recap", size=OG,
                  key_parts=(player, team, season, matches, rating, goals, photo_url, team_logo))


# ---------------- Достижение 1080×1360: медаль в духе наград Apple Fitness
# редкость: (светлый металл, основной, глубокий, подпись, точек вокруг, блёсток)
RARITY = {
    "bronze": ("#F1B27A", "#B4703A", "#5A3216", "Бронза", 0, 0),
    "silver": ("#F4F6FA", "#AEB4C2", "#5C6272", "Серебро", 0, 1),
    "gold": ("#FFE39A", "#E0A93A", "#7A4E0E", "Золото", 36, 2),
    "platinum": ("#E9F6FF", "#8EC1EA", "#2E5C86", "Платина", 48, 3),
    "secret": ("#E2D4FF", "#9B7BEA", "#3F2580", "Секретное", 36, 2),
    "legendary": ("#FFE2A0", "#C77DFF", "#4B1D8F", "Легендарное", 60, 5),
}
RARITY_ACCENT = {"bronze": "orange", "silver": "blue", "gold": "amber", "platinum": "blue", "secret": "violet", "legendary": "pink"}


def badge_card(*, username: str, name: str, description: str, rarity: str, secret: bool, date_label: str) -> str:
    import math

    top, bot, deep, label, n_dots, n_sparkles = RARITY.get(rarity, RARITY["bronze"])
    cx, cy = 540, 540
    dots = [{"x": round(cx + 290 * math.cos(2 * math.pi * k / n_dots)), "y": round(cy + 290 * math.sin(2 * math.pi * k / n_dots)),
             "r": 4 if k % 3 else 6, "o": ".9" if k % 3 == 0 else ".45"} for k in range(n_dots)]
    sparkles = []
    # Блёстки только сверху и по бокам: снизу подпись редкости.
    for k, deg in enumerate((-140, -35, 175, 5, -95)[:n_sparkles]):
        a = math.radians(deg)
        x, y, sz = cx + 330 * math.cos(a), cy + 320 * math.sin(a), 14 + 8 * (k % 2)
        sparkles.append({"x": round(x), "y": round(y), "x1": round(x - sz), "x2": round(x + sz), "y1": round(y - sz), "y2": round(y + sz)})
    name_size = fit(name, "display", 72, 1800, 44)
    names = wrap(name, name_size, 900, "display", 2)
    name_lines = [{"text": t, "y": 960 + i * (name_size + 6)} for i, t in enumerate(names)]
    y = name_lines[-1]["y"] + 60
    desc = wrap(description, 30, 860, "semibold", 3)
    ctx = {"accent_name": RARITY_ACCENT.get(rarity, "violet"), "top": top, "bot": bot, "deep": deep, "rarity_label": label,
           "rarity_y": 880, "dots": dots, "sparkles": sparkles, "secret": secret, "name_lines": name_lines,
           "name_size": name_size, "desc_lines": [{"text": t, "y": y + i * 42} for i, t in enumerate(desc)],
           "footer_note": f"@{username}" + (f" · {date_label}" if date_label else "")}
    return render("cards/badge.svg", ctx, prefix="badge", size=(1080, 1360),
                  key_parts=(username, name, description, rarity, secret, date_label))


# ---------------- Сторис 1080×1920
def story(*, accent_name: str, chip: str, number: str, label: str, sub: str = "", footer_note: str = "",
          image_url: str = "", progress: float = 0.72) -> str:
    image = image_data(image_url, 480)
    if image:  # фото в кольце, число под ним
        size = fit(number, "display", 200, 900, 90)
        number_y = 1240 + int(size * 0.72)
        label_top = number_y + 98
    else:  # число в центре кольца
        size = fit(number, "display", 320, 780, 120)
        number_y = 870 + int(size * 0.36)
        label_top = 1270
    labels = [{"text": t, "y": label_top + i * 72} for i, t in enumerate(wrap(label, 60, 920, "display700", 2))]
    y = labels[-1]["y"] + 64
    subs = [{"text": t, "y": y + i * 50} for i, t in enumerate(wrap(sub, 38, 920, "semibold", 2))] if sub else []
    chip = sentence(chip)
    chip_w = int(measure(chip, 27, "semibold")) + 84
    x = 540 - chip_w // 2
    ctx = {"accent_name": accent_name, "chip": chip, "chip_w": chip_w, "chip_x": x, "chip_dot": x + 32, "chip_text_x": x + 52,
           "number": number, "number_size": size, "number_y": number_y, "number_tracking": f"{-size * 0.045:.1f}",
           "label_lines": labels, "sub_lines": subs, "footer_note": footer_note, "image": image,
           "arc": int(1697 * max(0.05, min(progress, 1.0)))}
    return render("cards/story.svg", ctx, prefix="story", size=(1080, 1920),
                  key_parts=(accent_name, chip, number, label, sub, footer_note, image_url, progress))
