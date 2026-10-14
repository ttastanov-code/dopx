# core/management/commands/check_mobile_layout.py
"""manage.py check_mobile_layout [--base-url URL] [--widths 375,320] [--only /path/ ...] [--dashboard] [--screenshots DIR]

Мобильная вёрстка основных страниц в Chrome (Playwright): наложения текстов, текст за пределами
блока и за краем экрана, обрезка многоточием (запрещена), горизонтальная прокрутка. Нужен запущенный сервер (по умолчанию dev).
Страницы — из URL-резолвера с примерами объектов; вкладки номинаций проверяются каждая.
--dashboard — страницы staff-дашборда (сессия суперпользователя с пройденной 2FA), ширины 375 и 1440.
--screenshots DIR — сохранить снимки страниц во всю высоту, чтобы посмотреть глазами.
Код выхода 1, если есть проблемы. Запускать перед релизом (docs/DEPLOYMENT.md).
"""
from __future__ import annotations

import itertools
import re
from collections import defaultdict
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.urls import URLResolver, get_resolver

SCAN_JS = Path(__file__).resolve().parents[2] / "layout_check.js"

BLOCK_TAGS = {"div", "section", "article", "main", "aside", "nav", "ul", "ol", "table", "form", "header", "footer"}


def unbalanced_blocks(html: str) -> list[str]:
    """Лишние или незакрытые блочные теги: браузер их молча чинит, а раскладка ломается."""
    from html.parser import HTMLParser

    problems: list[str] = []
    stack: list[tuple[str, int]] = []

    class P(HTMLParser):
        def handle_starttag(self, tag, attrs):
            if tag in BLOCK_TAGS:
                stack.append((tag, self.getpos()[0]))

        def handle_endtag(self, tag):
            if tag not in BLOCK_TAGS:
                return
            if stack and stack[-1][0] == tag:
                stack.pop()
            elif any(t == tag for t, _l in stack):
                while stack and stack[-1][0] != tag:
                    t, line = stack.pop()
                    problems.append(f"незакрытый <{t}> (строка {line})")
                stack.pop()
            else:
                problems.append(f"лишний </{tag}> (строка {self.getpos()[0]})")

    P(convert_charrefs=True).feed(html)
    problems += [f"незакрытый <{t}> (строка {line})" for t, line in stack]
    return problems[:5]

# Служебные, частичные и нестраничные адреса.
SKIP_PARTS = (
    "admin/", "api/", "staff/", "__debug__", "captcha", "bot/", "tg/", "partial", "sitemap", "robots",
    "analytics", "widget", "embed", ".json", "manifest", "sw.js", "export", "logout", "share-card", "og/",
    ".png", ".svg", "ics", "feed", "push", "oauth", "verify-email/", "password-reset/<", "reset/<",
)
# Страницы только для вошедших — гостем не проверяем.
AUTH_ONLY = ("/users/profile", "/notifications", "/friends", "/invite", "/users/achievements", "/evaluations/")
COOKIE_NOTICE_ACK = "dopx_cookie_notice_ack_v1"
TEMP_USERNAME = "layout_check_bot"


def _walk(resolver, prefix=""):
    for p in resolver.url_patterns:
        route = prefix + str(p.pattern)
        if isinstance(p, URLResolver):
            yield from _walk(p, route)
        else:
            yield route


def _samples() -> dict:
    """Примеры объектов для параметров URL: разные статусы матчей и самые длинные имена."""
    from django.db.models.functions import Length

    from coaches.models import Coach
    from matches.models import Match
    from players.models import Player
    from referees.models import Referee
    from seasons.models import Season
    from teams.models import Team

    season = Season.get_primary_active() or Season.objects.order_by("-year").first()
    matches = [m for m in (
        Match.objects.filter(status="finished").order_by("-start_time").first(),
        Match.objects.filter(status="scheduled").order_by("start_time").first(),
        Match.objects.filter(status="postponed").first(),
        Match.objects.filter(status="live").first(),
    ) if m]
    teams = list(Team.objects.filter(teamseason__season=season).annotate(n=Length("name")).order_by("-n")[:2]) if season else []
    players = list(Player.objects.filter(matchlineupplayer__isnull=False).exclude(photo="")
                   .annotate(n=Length("first_name") + Length("last_name")).order_by("-n").distinct()[:2])
    coach = Coach.objects.filter(is_active=True).annotate(n=Length("last_name")).order_by("-n").first()
    referee = Referee.objects.filter(is_active=True).first()
    return {
        "match_id": [m.id for m in matches], "player_id": [p.id for p in players], "team_id": [t.id for t in teams],
        "coach_id": [coach.id] if coach else [], "referee_id": [referee.id] if referee else [],
        "season_id": [season.id] if season else [], "league_id": [season.league_id] if season else [],
        "tour": [_last_tour(season)] if season else [],
        # pk/id — по первому сегменту пути.
        "matches": [m.id for m in matches], "players": [p.id for p in players], "teams": [t.id for t in teams],
        "coaches": [coach.id] if coach else [], "referees": [referee.id] if referee else [],
        "leagues": [season.league_id] if season else [],
    }


def _last_tour(season):
    from matches.models import Match

    return (Match.objects.filter(season=season, status="finished", tour__isnull=False)
            .order_by("-tour").values_list("tour", flat=True).first() or 1)


DASHBOARD_PREFIX = "staff/dashboard/"


def collect_urls(username: str | None, dashboard: bool = False) -> list[str]:
    samples = _samples()
    if username:
        samples["username"] = [username]
    urls = set()
    for route in _walk(get_resolver()):
        if dashboard:
            if not route.startswith(DASHBOARD_PREFIX) or "2fa" in route or "partial" in route or "frame" in route:
                continue
        elif any(part in route for part in SKIP_PARTS):
            continue
        if "(?P" in route or "^" in route:
            continue
        params = re.findall(r"<(?:\w+:)?(\w+)>", route)
        fills = []
        for name in params:
            key = route.split("/")[0] if name in ("pk", "id") else name
            if not samples.get(key):
                break
            fills.append(samples[key])
        else:
            for combo in itertools.product(*fills):
                url = route
                for name, value in zip(params, combo):
                    url = re.sub(r"<(?:\w+:)?" + name + ">", str(value), url, count=1)
                urls.add("/" + url)
    return sorted(urls)


class Command(BaseCommand):
    help = "Проверка мобильной вёрстки основных страниц (Playwright, нужен запущенный сервер)."

    def add_arguments(self, parser):
        parser.add_argument("--base-url", default="http://127.0.0.1:8000")
        parser.add_argument("--widths", default="375,320", help="Ширины экрана через запятую.")
        parser.add_argument("--only", nargs="*", help="Проверить только эти пути.")
        parser.add_argument("--dashboard", action="store_true", help="Проверить страницы staff-дашборда.")
        parser.add_argument("--screenshots", default=None, help="Папка для снимков страниц во всю высоту.")
        parser.add_argument("--user", default=None, help="Логин для проверки как вошедший (по умолчанию временный служебный аккаунт).")

    def handle(self, *args, **opts):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise CommandError("Нужен playwright: pip install -r requirements-dev.txt") from exc

        from django.contrib.auth import get_user_model

        User = get_user_model()
        dashboard = opts["dashboard"]
        # Временный служебный пользователь: открытие страниц меняет аккаунт (серия дней, задания) — живых не трогаем.
        temp_user = None
        if opts["user"]:
            user = User.objects.filter(username=opts["user"]).first()
        else:
            user = temp_user = self._temp_user(User, staff=dashboard)
        if dashboard and (not user or not user.is_staff):
            raise CommandError("Для --dashboard нужен сотрудник: --user <логин>")
        urls = opts["only"] or collect_urls(user.username if user else None, dashboard=dashboard)
        widths = [int(w) for w in ("375,1440" if dashboard and opts["widths"] == "375,320" else opts["widths"]).split(",") if w.strip()]
        session_key = self._login_session(user, otp=dashboard) if user else None
        self.stdout.write(f"Страниц: {len(urls)}, ширины: {widths}, вход: {user.username if user else 'нет'}")
        try:
            # ORM — только до и после: внутри sync_playwright свой цикл событий.
            problems = self._scan(sync_playwright, opts["base_url"].rstrip("/"), urls, widths, session_key,
                                  guests=not dashboard, shots=opts["screenshots"])
        finally:
            if session_key:
                from django.contrib.sessions.models import Session
                Session.objects.filter(session_key=session_key).delete()
            if temp_user is not None:
                temp_user.delete()
        self._report(problems)
        if problems:
            raise SystemExit(1)

    @staticmethod
    def _temp_user(User, staff: bool):
        """Служебный аккаунт на время прогона (профиль заполнен; для дашборда — суперпользователь с 2FA)."""
        from django.utils import timezone

        User.objects.filter(username=TEMP_USERNAME).delete()
        user = User.objects.create(username=TEMP_USERNAME, email="layout-check@dopx-check.kz", is_verified=True,
                                   email_verified_at=timezone.now(), city="Алматы",
                                   is_staff=staff, is_superuser=staff)
        user.set_unusable_password()
        user.save(update_fields=["password"])
        if staff:
            from django_otp.plugins.otp_totp.models import TOTPDevice
            TOTPDevice.objects.create(user=user, name="layout-check", confirmed=True)
        return user

    @staticmethod
    def _login_session(user, otp: bool = False) -> str:
        """Временная сессия вместо ввода пароля; удаляется после прогона. otp — отметка пройденной 2FA."""
        from django.contrib.auth import BACKEND_SESSION_KEY, HASH_SESSION_KEY, SESSION_KEY
        from django.contrib.sessions.backends.db import SessionStore

        store = SessionStore()
        store[SESSION_KEY] = str(user.pk)
        store[BACKEND_SESSION_KEY] = "django.contrib.auth.backends.ModelBackend"
        store[HASH_SESSION_KEY] = user.get_session_auth_hash()
        if otp:
            from django_otp import DEVICE_ID_SESSION_KEY, devices_for_user

            device = next(iter(devices_for_user(user, confirmed=True)), None)
            if device is not None:
                store[DEVICE_ID_SESSION_KEY] = device.persistent_id
        store.create()
        return store.session_key

    def _scan(self, sync_playwright, base, urls, widths, session_key, guests: bool = True, shots: str | None = None) -> dict:
        js = SCAN_JS.read_text(encoding="utf-8")
        if shots:
            Path(shots).mkdir(parents=True, exist_ok=True)
        host = re.sub(r"^https?://", "", base).split("/")[0].split(":")[0]
        problems: dict = defaultdict(list)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="chrome", headless=True)
            for width in widths:
                for logged in ([True, False] if session_key and guests else [bool(session_key)]):
                    mobile = width < 768
                    ctx = browser.new_context(viewport={"width": width, "height": 800}, device_scale_factor=2 if mobile else 1,
                                              is_mobile=mobile, has_touch=mobile, locale="ru-RU")
                    ctx.add_init_script(f"try {{ localStorage.setItem('{COOKIE_NOTICE_ACK}', '1') }} catch (e) {{}}")
                    if logged:
                        ctx.add_cookies([{"name": settings.SESSION_COOKIE_NAME, "value": session_key,
                                          "domain": host, "path": "/"}])
                    for url in urls:
                        if not logged and url.startswith(AUTH_ONLY):
                            continue
                        label = f"{width}px{'' if logged else ', гость'} {url}"
                        # Своя вкладка на страницу: переадресация шагов мастера не обрывает следующую.
                        page = ctx.new_page()
                        try:
                            resp = self._open(page, base + url)
                            # Ошибка сервера — тоже проблема, а не пропуск.
                            if resp and resp.status >= 400:
                                problems[label] = [f"страница отвечает ошибкой HTTP {resp.status}"]
                                self.stdout.write(f"  ✗ {label}")
                                page.close()
                                continue
                            if (not resp or resp.status != 200 or "text/html" not in resp.headers.get("content-type", "")
                                    or resp.request.redirected_from):
                                page.close()
                                continue
                            page.wait_for_timeout(500)
                            page.evaluate("document.getElementById('djDebug')?.remove()")
                            found = [f"разметка: {m}" for m in unbalanced_blocks(resp.text())]
                            found += self._findings(page.evaluate(js))
                            if shots:
                                name = re.sub(r"[^a-z0-9]+", "_", url.lower()).strip("_") or "home"
                                page.screenshot(path=str(Path(shots) / f"{width}{'' if logged else '_guest'}_{name}.png"), full_page=True)
                            for radio in page.eval_on_selector_all(".dx-noms__radio", "els => els.map(e => e.id)"):
                                page.evaluate("id => { const r = document.getElementById(id); r.checked = true; }", radio)
                                page.wait_for_timeout(150)
                                found += [f"[вкладка {radio}] {f}" for f in self._findings(page.evaluate(js))]
                        except Exception as exc:  # noqa: BLE001 — одна страница не валит прогон
                            found = [f"ошибка загрузки: {str(exc).splitlines()[0][:150]}"]
                        page.close()
                        if found:
                            problems[label] = found
                        self.stdout.write(f"  {'✗' if found else '✓'} {label}")
                    ctx.close()
            browser.close()
        return problems

    @staticmethod
    def _open(page, url):
        """Загрузка с одним повтором: dev-сервер иногда обрывает соединение под нагрузкой."""
        try:
            return page.goto(url, wait_until="networkidle", timeout=30000)
        except Exception as exc:  # noqa: BLE001
            if "ERR_HTTP_RESPONSE_CODE_FAILURE" in str(exc) or "Download is starting" in str(exc):
                return None  # только POST (405), выгрузки файлов — не страница
            page.wait_for_timeout(1000)
            return page.goto(url, wait_until="networkidle", timeout=30000)

    @staticmethod
    def _findings(res: dict) -> list[str]:
        out = []
        if res.get("hscroll"):
            out.append(f"горизонтальная прокрутка: ширина страницы {res['hscroll']}px")
        out += [f"наложение: «{o['at']}» ({o['a']}) ⟂ «{o['bt']}» ({o['b']}), {o['w']}×{o['h']}px" for o in res["overlaps"]]
        out += [f"вылезает из блока на {o['over']}px: «{o['text']}» ({o['sel']})" for o in res["spills"]]
        out += [f"текст за краем карточки на {o['over']}px: «{o['text']}» ({o['sel']})" for o in res.get("outbox", [])]
        out += [f"иконка поверх текста: «{o['text']}» ({o['sel']})" for o in res.get("iconHits", [])]
        out += [f"за краем экрана: «{o['text']}» ({o['sel']})" for o in res["offscreen"]]
        out += [f"обрезан многоточием: «{o['text']}» ({o['sel']})" for o in res.get("truncated", [])]
        return out

    def _report(self, problems: dict) -> None:
        if not problems:
            self.stdout.write(self.style.SUCCESS("Проблем не найдено."))
            return
        for label, found in problems.items():
            self.stdout.write(self.style.WARNING(label))
            for line in found:
                self.stdout.write(f"    {line}")
        self.stdout.write(self.style.ERROR(f"Страниц с проблемами: {len(problems)}"))
