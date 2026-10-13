# core/tests_layout_check.py
"""manage.py check_mobile_layout: сбор страниц и скрипт проверки вёрстки."""
from __future__ import annotations

import unittest
from datetime import timedelta

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from core.management.commands.check_mobile_layout import SCAN_JS, collect_urls

BROKEN_HTML = """<html><body style="margin:0">
<div style="display:flex;width:200px"><span style="white-space:nowrap;width:80px">Самый зрелищный матч</span><b>7,0</b></div>
<div style="width:60px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis">Обрезанный многоточием текст</div>
<details><summary>свёрнуто</summary><p style="margin-top:-20px">скрытый текст</p></details>
<div style="width:100px"><span style="white-space:nowrap">Очень длинный текст за краем экрана шириной больше экрана</span></div>
</body></html>"""


def _chrome_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            pw.chromium.launch(channel="chrome", headless=True).close()
        return True
    except Exception:  # noqa: BLE001 — нет playwright или Chrome: тест пропускаем
        return False


class CollectUrlsTests(TestCase):
    def test_pages_with_sample_objects(self):
        from leagues.models import League
        from matches.models import Match
        from seasons.models import Season
        from teams.models import Team

        league = League.objects.create(name="Премьер-лига", country="KZ", is_primary=True)
        season = Season.objects.create(league=league, year="2026", is_active=True)
        match = Match.objects.create(
            league=league, season=season, home_team=Team.objects.create(name="Кайрат"),
            away_team=Team.objects.create(name="Иртыш"), start_time=timezone.now() - timedelta(days=1),
            voting_open_until=timezone.now(), status="finished", tour=3,
        )
        urls = collect_urls(None)
        self.assertIn("/", urls)
        self.assertIn(f"/matches/{match.id}/", urls)
        self.assertFalse([u for u in urls if u.startswith(("/admin/", "/api/", "/staff/")) or "<" in u])


@unittest.skipUnless(_chrome_available(), "нужны playwright и Chrome")
class LayoutScanScriptTests(SimpleTestCase):
    def test_finds_real_problems_and_ellipsis(self):
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="chrome", headless=True)
            page = browser.new_page(viewport={"width": 320, "height": 600})
            page.set_content(BROKEN_HTML)
            res = page.evaluate(SCAN_JS.read_text(encoding="utf-8"))
            browser.close()
        self.assertEqual([(o["at"], o["bt"]) for o in res["overlaps"]], [("Самый зрелищный матч", "7,0")])
        self.assertTrue(res["offscreen"])
        self.assertTrue(res["hscroll"])
        texts = " ".join(o["text"] for o in res["spills"] + res["offscreen"])
        self.assertNotIn("многоточием", texts)
        self.assertNotIn("скрытый", texts)
        # Многоточие — отдельная находка: текст должен быть виден целиком.
        self.assertEqual([o["text"] for o in res["truncated"]], ["Обрезанный многоточием текст"])
