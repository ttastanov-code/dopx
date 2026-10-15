# core/cards_html.py
"""Карточки для соцсетей из HTML/CSS: Chromium (Playwright) делает снимок в двойном разрешении.
Шаблоны — templates/cards/html/, шрифты Inter встраиваются в страницу. Нет Chromium — None, вызывающий рисует SVG (core.cards)."""
from __future__ import annotations

import base64
import hashlib
import logging
from functools import lru_cache
from pathlib import Path

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.template.loader import render_to_string

logger = logging.getLogger(__name__)

SCALE = 2  # PNG 2400×1260 для OG 1200×630: чётко на телефонах
FONT_DIR = Path(settings.BASE_DIR) / "static" / "fonts" / "cards"
TEMPLATE_DIR = Path(settings.BASE_DIR) / "templates" / "cards" / "html"
FONT_FACES = (("Inter Display", 800, "InterDisplay-ExtraBold.ttf"), ("Inter Display", 700, "InterDisplay-Bold.ttf"),
              ("Inter Display", 500, "InterDisplay-Medium.ttf"), ("Inter", 800, "Inter-ExtraBold.ttf"),
              ("Inter", 600, "Inter-SemiBold.ttf"), ("Inter", 400, "Inter-Regular.ttf"))


@lru_cache(maxsize=1)
def font_css() -> str:
    out = []
    for family, weight, file in FONT_FACES:
        data = base64.b64encode((FONT_DIR / file).read_bytes()).decode()
        out.append(f"@font-face{{font-family:'{family}';font-weight:{weight};src:url(data:font/ttf;base64,{data}) format('truetype');}}")
    return "\n".join(out)


@lru_cache(maxsize=1)
def icon_font_css() -> str:
    """Шрифт Tabler для иконок медалей — встраивается в страницу карточки."""
    font = Path(settings.BASE_DIR) / "static" / "vendor" / "tabler-icons" / "fonts" / "tabler-icons.woff2"
    data = base64.b64encode(font.read_bytes()).decode()
    return f"@font-face{{font-family:'tabler';src:url(data:font/woff2;base64,{data}) format('woff2');}}"


@lru_cache(maxsize=None)
def icon_char(name: str) -> str:
    """Символ иконки Tabler по имени (берётся из tabler-icons.min.css)."""
    import re

    css = (Path(settings.BASE_DIR) / "static" / "vendor" / "tabler-icons" / "tabler-icons.min.css").read_text()
    m = re.search(r"\.ti-" + re.escape(name) + r':before\{content:"\\([0-9a-f]+)"', css)
    return chr(int(m.group(1), 16)) if m else ""


def _templates_hash() -> str:
    digest = hashlib.sha1()
    for f in sorted(TEMPLATE_DIR.glob("*.html")):
        digest.update(f.read_bytes())
    return digest.hexdigest()[:10]


class _Renderer:
    """Один Chromium на процесс в своём потоке: запуск ~1–3 с платим один раз, дальше карточка ~0,3 с.
    Sync-API Playwright привязан к потоку, поэтому запросы из потоков Django идут через очередь."""

    def __init__(self):
        import queue
        import threading

        self.jobs = queue.Queue()
        threading.Thread(target=self._loop, name="cards-chromium", daemon=True).start()

    def _launch(self, pw):
        try:
            return pw.chromium.launch(args=["--no-sandbox", "--font-render-hinting=none"])
        except Exception:  # на ноутбуке нет скачанного Chromium — берём установленный Chrome
            return pw.chromium.launch(channel="chrome", args=["--font-render-hinting=none"])

    def _loop(self):
        from playwright.sync_api import sync_playwright

        pw = browser = None
        while True:
            html, size, future = self.jobs.get()
            try:
                if browser is None or not browser.is_connected():
                    pw = pw or sync_playwright().start()
                    browser = self._launch(pw)
                page = browser.new_page(viewport={"width": size[0], "height": size[1]}, device_scale_factor=SCALE)
                try:
                    page.set_content(html, wait_until="load")
                    page.evaluate("document.fonts.ready.then(() => window.__fit && window.__fit())")
                    future.set_result(page.screenshot(type="png", clip={"x": 0, "y": 0, "width": size[0], "height": size[1]}))
                finally:
                    page.close()
            except Exception as e:
                browser = None  # следующий запрос поднимет браузер заново
                future.set_exception(e)

    def shot(self, html: str, size: tuple[int, int], timeout: float = 60) -> bytes:
        from concurrent.futures import Future

        future = Future()
        self.jobs.put((html, size, future))
        return future.result(timeout=timeout)


_renderer = None


def _screenshot(html: str, size: tuple[int, int]) -> bytes:
    global _renderer
    if _renderer is None:
        _renderer = _Renderer()
    return _renderer.shot(html, size)


def render(template: str, context: dict, *, prefix: str, key_parts: tuple, size: tuple[int, int]) -> str | None:
    """HTML-шаблон → PNG в MEDIA (кэш по содержимому). None — Chromium недоступен."""
    key = hashlib.sha256("|".join(map(str, ("html", _templates_hash(), template, size, *key_parts))).encode()).hexdigest()[:24]
    path = f"share-cards/{prefix}_h{key}.png"
    if default_storage.exists(path):
        return path
    html = render_to_string(template, {"w": size[0], "h": size[1], "font_css": font_css(), **context})
    try:
        png = _screenshot(html, size)
    except Exception as e:
        logger.warning("cards_html: Chromium не отрисовал %s: %s", template, e)
        return None
    return default_storage.save(path, ContentFile(png))
