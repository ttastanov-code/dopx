# partners/tests.py
"""
Первый набор тестов для partners — до этой сессии у приложения не было
tests.py вообще, несмотря на то, что здесь живут три чувствительные к
безопасности вещи: подписанная cookie реферальной атрибуции (dopx_ref),
приватный токен контент-фида (Partner.feed_token) и CSP-раскрытие для
embed-виджетов на чужих доменах. Ошибка в любой из них — это либо
подделываемая партнёрская атрибуция (комиссии/отчётность не по факту
перехода), либо утечка приватного фида через чей-то прокси-кэш, либо
виджет, который либо не грузится ни у кого, либо грузится где угодно.

CACHES переопределён на LocMemCache (см. core/tests.py) везде, где
затрагивается is_rate_limited — прод использует Redis (dopx/settings.py::
CACHES), тест не должен зависеть от поднятого Redis-сервера.

CELERY_TASK_ALWAYS_EAGER=True (см. analytics/tests.py) везде, где тест
проверяет запись AnalyticsEvent — track_event() шлёт событие через
persist_event_task.delay(), без eager-режима запись просто не произойдёт
синхронно и тест либо упадёт, либо (хуже) будет молча проверять пустой
queryset.

БАГ, КОТОРЫЙ ТУТ БЫЛ (найден при написании этого файла, 2026-08-28):
Banner.is_currently_active() (partners/models.py) проверял только
собственные поля баннера (is_active/starts_at/ends_at) — деактивация
Partner в админке никак не останавливала уже включённые баннеры этого
партнёра. Пофикшено там же, докстринг с деталями — в partners/models.py::
Banner.is_currently_active. BannerActiveWindowTests.
test_inactive_partner_excludes_banner ниже — регрессионный тест на это.
"""
from __future__ import annotations

from datetime import timedelta

from django.core.cache import cache
from django.core.signing import BadSignature
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from analytics.models import AnalyticsEvent, EventName
from leagues.models import League
from matches.models import Match
from partners.models import Banner, BannerZone, Partner
from partners.services import (
    REFERRAL_COOKIE_NAME,
    get_active_banner_for_zone,
)
from players.models import Player
from seasons.models import Season
from teams.models import Team

LOCMEM_CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "test-partners-ratelimit",
    }
}


# ============================================================
# Banner.is_currently_active() / get_active_banner_for_zone — чистая логика
# ротации, без HTTP.
# ============================================================

class BannerActiveWindowTests(TestCase):
    """is_currently_active() — единственное место, где решается, показывать
    ли баннер прямо сейчас (partners/services.py::get_active_banner_for_zone
    и admin.py::ActivelyShowingFilter/is_currently_active_badge)."""

    def setUp(self):
        self.partner = Partner.objects.create(name="Bookmaker LLC", slug="bookmaker-llc")

    def make_banner(self, **kwargs):
        defaults = dict(
            zone=BannerZone.SIDEBAR, title="Test Banner", target_url="https://example.com/",
            is_active=True,
        )
        defaults.update(kwargs)
        return Banner.objects.create(**defaults)

    def test_inactive_flag_excludes_banner(self):
        banner = self.make_banner(is_active=False)
        self.assertFalse(banner.is_currently_active())

    def test_before_start_window_excluded(self):
        banner = self.make_banner(starts_at=timezone.now() + timedelta(days=1))
        self.assertFalse(banner.is_currently_active())

    def test_after_end_window_excluded(self):
        banner = self.make_banner(ends_at=timezone.now() - timedelta(days=1))
        self.assertFalse(banner.is_currently_active())

    def test_within_window_included(self):
        banner = self.make_banner(
            starts_at=timezone.now() - timedelta(days=1),
            ends_at=timezone.now() + timedelta(days=1),
        )
        self.assertTrue(banner.is_currently_active())

    def test_no_window_bounds_included_when_active(self):
        banner = self.make_banner()
        self.assertTrue(banner.is_currently_active())

    def test_inactive_partner_excludes_banner(self):
        """Регрессия на БАГ, КОТОРЫЙ ТУТ БЫЛ — см. докстринг модуля и
        partners/models.py::Banner.is_currently_active. Партнёр
        деактивирован, сам баннер формально всё ещё is_active=True —
        баннер обязан перестать показываться."""
        self.partner.is_active = False
        self.partner.save(update_fields=["is_active"])
        banner = self.make_banner(partner=self.partner)
        self.assertFalse(banner.is_currently_active())

    def test_active_partner_banner_included(self):
        banner = self.make_banner(partner=self.partner)
        self.assertTrue(banner.is_currently_active())

    def test_banner_without_partner_unaffected_by_partner_status(self):
        """partner=None — собственное промо DOPX, см. Banner.partner.help_text.
        Нет партнёра — нечему становиться is_active=False, окно/is_active
        самого баннера — единственные критерии."""
        banner = self.make_banner(partner=None)
        self.assertTrue(banner.is_currently_active())


class GetActiveBannerForZoneTests(TestCase):
    """Интеграционный уровень поверх is_currently_active() — сам селектор
    из partners/services.py, который дёргает шаблонный тег render_banner."""

    def test_returns_none_when_no_banners_in_zone(self):
        self.assertIsNone(get_active_banner_for_zone(BannerZone.HOME_HERO))

    def test_returns_none_when_only_inactive_partner_banners_exist(self):
        partner = Partner.objects.create(name="Suspended Partner", slug="suspended", is_active=False)
        Banner.objects.create(
            partner=partner, zone=BannerZone.LEADERBOARD, title="Dead ad",
            target_url="https://example.com/", is_active=True,
        )
        self.assertIsNone(get_active_banner_for_zone(BannerZone.LEADERBOARD))

    def test_wrong_zone_not_returned(self):
        Banner.objects.create(
            zone=BannerZone.MATCH_DETAIL, title="Match banner",
            target_url="https://example.com/", is_active=True,
        )
        self.assertIsNone(get_active_banner_for_zone(BannerZone.SIDEBAR))

    def test_single_active_candidate_is_returned(self):
        banner = Banner.objects.create(
            zone=BannerZone.SIDEBAR, title="Only one", target_url="https://example.com/", is_active=True,
        )
        self.assertEqual(get_active_banner_for_zone(BannerZone.SIDEBAR).id, banner.id)


# ============================================================
# /go/<slug>/ — PartnerReferralRedirectView. Подписанная cookie атрибуции —
# главный объект внимания этого файла.
# ============================================================

@override_settings(CACHES=LOCMEM_CACHES, CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
class ReferralRedirectViewTests(TestCase):
    def setUp(self):
        cache.clear()
        self.partner = Partner.objects.create(name="Media Partner", slug="media-partner")

    def _url(self, slug=None, **params):
        url = reverse("partners:referral_redirect", args=[slug or self.partner.slug])
        if params:
            from urllib.parse import urlencode
            url = f"{url}?{urlencode(params)}"
        return url

    def test_redirects_to_home_by_default(self):
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("core:home"))

    def test_unknown_slug_returns_404(self):
        response = self.client.get(reverse("partners:referral_redirect", args=["no-such-partner"]))
        self.assertEqual(response.status_code, 404)

    def test_inactive_partner_returns_404(self):
        self.partner.is_active = False
        self.partner.save(update_fields=["is_active"])
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 404)

    def test_valid_relative_next_is_honored(self):
        response = self.client.get(self._url(**{"next": "/leaders/"}))
        self.assertEqual(response.url, "/leaders/")

    def test_external_next_is_rejected_open_redirect(self):
        """БАГ-класс, который здесь СОЗНАТЕЛЬНО закрыт кодом
        (url_has_allowed_host_and_scheme) — ?next=https://evil.example/
        не должен превратить партнёрскую ссылку в open redirect."""
        response = self.client.get(self._url(**{"next": "https://evil.example/phish"}))
        self.assertEqual(response.url, reverse("core:home"))

    def test_sets_referral_cookie(self):
        response = self.client.get(self._url())
        self.assertIn(REFERRAL_COOKIE_NAME, response.cookies)

    def test_cookie_value_is_signed_not_plaintext_slug(self):
        """Значение cookie НЕ должно совпадать с самим slug'ом партнёра —
        если совпадает, значит cookie снова ставится обычным set_cookie
        (голым текстом), а не set_signed_cookie, и подпись отсутствует."""
        response = self.client.get(self._url())
        raw_value = response.cookies[REFERRAL_COOKIE_NAME].value
        self.assertNotEqual(raw_value, self.partner.slug)

    def test_cookie_round_trips_through_get_signed_cookie(self):
        """Правильная подпись читается обратно тем же способом, что и
        users/views.py::RegisterView.form_valid — get_signed_cookie с тем
        же salt='partners.referral' должен вернуть исходный slug."""
        response = self.client.get(self._url())
        raw_value = response.cookies[REFERRAL_COOKIE_NAME].value

        request = RequestFactory().get("/")
        request.COOKIES[REFERRAL_COOKIE_NAME] = raw_value
        decoded_slug = request.get_signed_cookie(REFERRAL_COOKIE_NAME, salt="partners.referral")
        self.assertEqual(decoded_slug, self.partner.slug)

    def test_forged_unsigned_cookie_is_rejected(self):
        """Ядро требования безопасности: атакующий вручную (в DevTools или
        скриптом, минуя /go/<slug>/ вообще) выставляет
        `dopx_ref=<slug другого партнёра>` без знания SECRET_KEY —
        get_signed_cookie обязан отклонить это подписью, а не тихо принять
        значение как валидное. Именно так — try/except BadSignature —
        читает cookie users/views.py::RegisterView.form_valid; форгед
        cookie там просто игнорируется (пользователь не привязывается ни к
        какому партнёру), а НЕ падает и НЕ засчитывает атрибуцию."""
        request = RequestFactory().get("/")
        request.COOKIES[REFERRAL_COOKIE_NAME] = "media-partner"  # сырой, неподписанный slug
        with self.assertRaises(BadSignature):
            request.get_signed_cookie(REFERRAL_COOKIE_NAME, salt="partners.referral")

    def test_tampered_signed_cookie_is_rejected(self):
        """Не просто отсутствие подписи, а её ПОДДЕЛКА: берём настоящую
        подписанную cookie (за другого партнёра можно было бы взять только
        его собственную честную ссылку) и меняем в ней один символ —
        HMAC должен не совпасть."""
        response = self.client.get(self._url())
        raw_value = response.cookies[REFERRAL_COOKIE_NAME].value
        tampered = raw_value[:-1] + ("a" if raw_value[-1] != "a" else "b")

        request = RequestFactory().get("/")
        request.COOKIES[REFERRAL_COOKIE_NAME] = tampered
        with self.assertRaises(BadSignature):
            request.get_signed_cookie(REFERRAL_COOKIE_NAME, salt="partners.referral")

    def test_cookie_is_httponly(self):
        response = self.client.get(self._url())
        self.assertTrue(response.cookies[REFERRAL_COOKIE_NAME]["httponly"])

    def test_cookie_secure_flag_matches_debug_setting(self):
        from django.conf import settings
        response = self.client.get(self._url())
        secure_flag = bool(response.cookies[REFERRAL_COOKIE_NAME]["secure"])
        self.assertEqual(secure_flag, not settings.DEBUG)

    def test_visit_is_tracked_in_analytics(self):
        self.client.get(self._url())
        count = AnalyticsEvent.objects.filter(
            event_name=EventName.PARTNER_REFERRAL_VISIT,
            properties__partner_slug=self.partner.slug,
        ).count()
        self.assertEqual(count, 1)

    def test_repeated_visits_beyond_rate_limit_are_not_all_tracked(self):
        """Скрипт, долбящий /go/<slug>/ в цикле с одного IP, не должен
        накручивать статистику визитов сверх PARTNER_STATS_RATE_LIMIT — но
        редирект (пользовательский опыт) остаётся плавным в любом случае."""
        from partners.views import PARTNER_STATS_RATE_LIMIT

        for _ in range(PARTNER_STATS_RATE_LIMIT + 5):
            response = self.client.get(self._url())
            self.assertEqual(response.status_code, 302)  # редирект не деградирует в 429/ошибку

        count = AnalyticsEvent.objects.filter(
            event_name=EventName.PARTNER_REFERRAL_VISIT,
            properties__partner_slug=self.partner.slug,
        ).count()
        self.assertEqual(count, PARTNER_STATS_RATE_LIMIT)


# ============================================================
# /ad/<uuid:pk>/click/ — BannerClickRedirectView
# ============================================================

@override_settings(CACHES=LOCMEM_CACHES, CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
class BannerClickRedirectViewTests(TestCase):
    def setUp(self):
        cache.clear()
        self.banner = Banner.objects.create(
            zone=BannerZone.SIDEBAR, title="Click me",
            target_url="https://bookmaker.example/promo", is_active=True,
        )

    def test_click_redirects_to_target_with_utm_params(self):
        response = self.client.get(reverse("partners:banner_click", args=[self.banner.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith("https://bookmaker.example/promo"))
        self.assertIn("utm_source=dopx", response.url)
        self.assertIn("utm_medium=banner", response.url)

    def test_missing_banner_returns_404(self):
        import uuid
        response = self.client.get(reverse("partners:banner_click", args=[uuid.uuid4()]))
        self.assertEqual(response.status_code, 404)

    def test_click_is_tracked_once(self):
        self.client.get(reverse("partners:banner_click", args=[self.banner.pk]))
        count = AnalyticsEvent.objects.filter(
            event_name=EventName.BANNER_CLICK,
            properties__banner_id=str(self.banner.id),
        ).count()
        self.assertEqual(count, 1)

    def test_repeated_clicks_beyond_rate_limit_are_capped_but_redirect_stays_smooth(self):
        from partners.views import PARTNER_STATS_RATE_LIMIT

        url = reverse("partners:banner_click", args=[self.banner.pk])
        for _ in range(PARTNER_STATS_RATE_LIMIT + 5):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 302)

        count = AnalyticsEvent.objects.filter(
            event_name=EventName.BANNER_CLICK,
            properties__banner_id=str(self.banner.id),
        ).count()
        self.assertEqual(count, PARTNER_STATS_RATE_LIMIT)


# ============================================================
# /partners/<slug>/feed/<uuid:token>/ — PartnerContentFeedView
# ============================================================

@override_settings(CACHES=LOCMEM_CACHES, CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
class PartnerContentFeedViewTests(TestCase):
    def setUp(self):
        cache.clear()
        self.partner = Partner.objects.create(name="Feed Partner", slug="feed-partner")

    def _url(self, slug=None, token=None):
        return reverse(
            "partners:content_feed",
            args=[slug or self.partner.slug, token or self.partner.feed_token],
        )

    def test_valid_token_returns_200(self):
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    def test_valid_token_response_has_no_store_cache_control(self):
        """Явная проверка заголовка — токен доступа лежит в самом URL,
        значит любое кэширование по цепочке (CDN/прокси на стороне
        партнёра) потенциально утекает приватную ссылку. no-store
        запрещает кэширование где бы то ни было."""
        response = self.client.get(self._url())
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")

    def test_wrong_token_returns_404_not_200(self):
        import uuid
        wrong_token = uuid.uuid4()
        self.assertNotEqual(wrong_token, self.partner.feed_token)
        response = self.client.get(self._url(token=wrong_token))
        self.assertEqual(response.status_code, 404)
        # Раз уж 404 — тело точно не должно содержать чужих данных партнёра.
        self.assertNotIn(b"Feed Partner", response.content)

    def test_wrong_token_does_not_leak_partner_existence_via_403(self):
        """404, а не 403 — код намеренно не подтверждает существование
        партнёра с этим slug тому, кто подбирает токен наугад (см. докстринг
        PartnerContentFeedView)."""
        import uuid
        response = self.client.get(self._url(token=uuid.uuid4()))
        self.assertEqual(response.status_code, 404)
        self.assertNotEqual(response.status_code, 403)

    def test_unknown_slug_returns_404(self):
        response = self.client.get(self._url(slug="no-such-partner"))
        self.assertEqual(response.status_code, 404)

    def test_inactive_partner_feed_returns_404_even_with_correct_token(self):
        self.partner.is_active = False
        self.partner.save(update_fields=["is_active"])
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 404)

    def test_feed_access_is_tracked(self):
        self.client.get(self._url())
        count = AnalyticsEvent.objects.filter(
            event_name=EventName.PARTNER_FEED_ACCESSED,
            properties__partner_slug=self.partner.slug,
        ).count()
        self.assertEqual(count, 1)

    def test_wrong_token_access_is_not_tracked_as_successful_feed_access(self):
        import uuid
        self.client.get(self._url(token=uuid.uuid4()))
        count = AnalyticsEvent.objects.filter(
            event_name=EventName.PARTNER_FEED_ACCESSED,
            properties__partner_slug=self.partner.slug,
        ).count()
        self.assertEqual(count, 0)

    def test_feed_contains_finished_matches(self):
        league = League.objects.create(name="Test League", country="KZ")
        season = Season.objects.create(league=league, year="2026", is_active=True)
        team_a = Team.objects.create(name="Team A")
        team_b = Team.objects.create(name="Team B")
        Match.objects.create(
            league=league, season=season, home_team=team_a, away_team=team_b,
            start_time=timezone.now() - timedelta(days=1), status="finished",
            # voting_open_until NOT NULL на уровне БД — matches/models.py.
            # Значение неважно для этого теста (лента фида не смотрит на
            # голосование), важно только что status="finished".
            voting_open_until=timezone.now() + timedelta(hours=48),
            home_score=2, away_score=1,
        )
        response = self.client.get(self._url())
        payload = response.json()
        self.assertEqual(payload["partner"], self.partner.name)
        self.assertEqual(len(payload["items"]), 1)
        self.assertIn("Team A", payload["items"][0]["caption"])

    def test_scheduled_match_not_included_in_feed(self):
        league = League.objects.create(name="Test League", country="KZ")
        season = Season.objects.create(league=league, year="2026", is_active=True)
        team_a = Team.objects.create(name="Team A")
        team_b = Team.objects.create(name="Team B")
        Match.objects.create(
            league=league, season=season, home_team=team_a, away_team=team_b,
            start_time=timezone.now() + timedelta(days=1), status="scheduled",
            # voting_open_until — NOT NULL на уровне модели (matches/models.py)
            # независимо от статуса; для scheduled-матча значение не имеет
            # никакого продуктового смысла (голосование появляется только
            # после финиша), но БД всё равно требует что-то валидное.
            voting_open_until=timezone.now() + timedelta(days=3),
        )
        response = self.client.get(self._url())
        self.assertEqual(response.json()["items"], [])


# ============================================================
# Feed-токен: две разные модели партнёров получают разные токены — иначе
# закрытый фид одного партнёра случайно совпал бы с фидом другого.
# ============================================================

class PartnerFeedTokenModelTests(TestCase):
    def test_feed_token_is_auto_generated(self):
        partner = Partner.objects.create(name="Auto Token", slug="auto-token")
        self.assertIsNotNone(partner.feed_token)

    def test_feed_tokens_are_unique_per_partner(self):
        partner_a = Partner.objects.create(name="Partner A", slug="partner-a")
        partner_b = Partner.objects.create(name="Partner B", slug="partner-b")
        self.assertNotEqual(partner_a.feed_token, partner_b.feed_token)


# ============================================================
# Embed-виджеты (players:widget / teams:widget / core:standings_widget) —
# сами view живут в других приложениях, но CSP-раскрытие (dopx/middleware.py)
# и трекинг embed-показа (partners/services.py::track_widget_embed_view) —
# прямая ответственность partners. По аналогии с
# season_squad/tests.py::WidgetEmbedTests (та же CSP-регрессия 2026-08-22:
# @xframe_options_exempt на view был, а разрешение в
# ContentSecurityPolicyMiddleware.WIDGET_PATH_PATTERN — нет, из-за чего
# frame-ancestors 'self' блокировал iframe у партнёра несмотря на рабочую
# ссылку).
# ============================================================

@override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
class WidgetEmbedCSPAndTrackingTests(TestCase):
    def setUp(self):
        self.team = Team.objects.create(name="Widget Team")
        self.player = Player.objects.create(first_name="Виджет", last_name="Игрок", team=self.team)

    def test_player_widget_allows_any_frame_ancestor(self):
        response = self.client.get(reverse("players:widget", args=[self.player.id]))
        self.assertEqual(response.status_code, 200)
        self.assertIn("frame-ancestors *", response.headers.get("Content-Security-Policy", ""))

    def test_team_widget_allows_any_frame_ancestor(self):
        response = self.client.get(reverse("teams:widget", args=[self.team.id]))
        self.assertEqual(response.status_code, 200)
        self.assertIn("frame-ancestors *", response.headers.get("Content-Security-Policy", ""))

    def test_standings_widget_allows_any_frame_ancestor(self):
        response = self.client.get(reverse("core:standings_widget"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("frame-ancestors *", response.headers.get("Content-Security-Policy", ""))

    def test_public_player_page_keeps_strict_frame_ancestors(self):
        """Сам сайт (не embed-путь) не должен по ошибке стать более
        открытым — только точечные /widget/-пути ослабляют CSP."""
        response = self.client.get(reverse("players:detail", args=[self.player.id]))
        self.assertIn("frame-ancestors 'self'", response.headers.get("Content-Security-Policy", ""))

    def test_player_widget_view_tracks_embed_with_referrer(self):
        """track_widget_embed_view (partners/services.py) кладёт
        HTTP_REFERER iframe-запроса в properties.embedder_referrer — это
        URL страницы ПАРТНЁРА, которая встроила виджет, а не наша
        страница; без этого поля вообще невозможно узнать, кто embed'ит."""
        self.client.get(
            reverse("players:widget", args=[self.player.id]),
            HTTP_REFERER="https://fan-community.kz/blog/best-player",
        )
        event = AnalyticsEvent.objects.get(
            event_name=EventName.WIDGET_EMBED_VIEWED,
            properties__widget_type="player",
        )
        self.assertEqual(event.properties["entity_id"], str(self.player.id))
        self.assertEqual(event.properties["embedder_referrer"], "https://fan-community.kz/blog/best-player")

    def test_team_widget_view_tracks_embed(self):
        self.client.get(reverse("teams:widget", args=[self.team.id]))
        count = AnalyticsEvent.objects.filter(
            event_name=EventName.WIDGET_EMBED_VIEWED,
            properties__widget_type="team",
            properties__entity_id=str(self.team.id),
        ).count()
        self.assertEqual(count, 1)
