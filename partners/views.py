# partners/views.py
from __future__ import annotations

from django.conf import settings
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from core.utils import get_client_ip, is_rate_limited

from .models import Banner, Partner
from .services import (
    REFERRAL_COOKIE_MAX_AGE,
    REFERRAL_COOKIE_NAME,
    build_click_redirect_url,
    build_content_feed,
    build_mood_index_feed,
    record_click,
    record_impression,
    track_partner_feed_access,
    track_partner_referral_visit,
)

# Лимит записи статистики переходов/кликов с одного IP. Редирект работает всегда.
PARTNER_STATS_RATE_LIMIT = 30
PARTNER_STATS_RATE_LIMIT_WINDOW_SECONDS = 60


class PartnerReferralRedirectView(View):
    """/go/<slug>/ — реферальная ссылка партнёра: логирует визит и ставит cookie атрибуции
    (читает RegisterView). ?next= проверяется от open redirect.
    """

    def get(self, request: HttpRequest, slug: str) -> HttpResponse:
        partner = get_object_or_404(Partner, slug=slug, is_active=True)

        next_path = request.GET.get("next", "")
        if next_path and url_has_allowed_host_and_scheme(
            next_path, allowed_hosts={request.get_host()}, require_https=request.is_secure()
        ):
            redirect_to = next_path
        else:
            redirect_to = reverse("core:home")

        # Сверх лимита визит не засчитываем, редирект всё равно делаем.
        client_ip = get_client_ip(request)
        if not client_ip or not is_rate_limited(
            f'partner_referral:{partner.slug}:{client_ip}',
            PARTNER_STATS_RATE_LIMIT, PARTNER_STATS_RATE_LIMIT_WINDOW_SECONDS,
        ):
            track_partner_referral_visit(partner, request, next_path=redirect_to)

        response = redirect(redirect_to)
        # Подписанная cookie — чужой slug не подставить.
        response.set_signed_cookie(
            REFERRAL_COOKIE_NAME,
            partner.slug,
            salt="partners.referral",
            max_age=REFERRAL_COOKIE_MAX_AGE,
            samesite="Lax",
            # httponly; secure — не в DEBUG.
            httponly=True,
            secure=not settings.DEBUG,
        )
        return response


class BannerClickRedirectView(View):
    """/ad/<uuid>/click/ — учёт клика по баннеру и редирект на target_url."""

    def get(self, request: HttpRequest, pk) -> HttpResponse:
        banner = get_object_or_404(Banner, pk=pk)
        # Лимит засчитывания клика, редирект всегда.
        client_ip = get_client_ip(request)
        if not client_ip or not is_rate_limited(
            f'banner_click:{banner.pk}:{client_ip}',
            PARTNER_STATS_RATE_LIMIT, PARTNER_STATS_RATE_LIMIT_WINDOW_SECONDS,
        ):
            record_click(banner, request)
        return redirect(build_click_redirect_url(banner))


@method_decorator(csrf_exempt, name="dispatch")
class BannerViewBeaconView(View):
    """/ad/<uuid>/view/ — sendBeacon из ads.js: баннер был виден на экране. Без CSRF: только счётчик."""

    def post(self, request: HttpRequest, pk) -> HttpResponse:
        banner = get_object_or_404(Banner, pk=pk)
        client_ip = get_client_ip(request)
        if client_ip and not is_rate_limited(
            f'banner_view:{banner.pk}:{client_ip}', PARTNER_STATS_RATE_LIMIT, PARTNER_STATS_RATE_LIMIT_WINDOW_SECONDS,
        ):
            record_impression(banner, request)
        return HttpResponse(status=204)


class PartnerContentFeedView(View):
    """/partners/<slug>/feed/<token>/ — JSON-фид ассетов по последним матчам.
    Доступ по Partner.feed_token в URL.
    """

    def get(self, request: HttpRequest, slug: str, token: str) -> HttpResponse:
        partner = get_object_or_404(Partner, slug=slug, is_active=True)
        if str(partner.feed_token) != str(token):
            # 404, а не 403 — не раскрываем существование партнёра.
            raise Http404()

        track_partner_feed_access(partner, request)
        items = build_content_feed(partner, request)
        response = JsonResponse({"partner": partner.name, "items": items})
        # no-store — токен в URL, ответ нигде не кэшируем.
        response["Cache-Control"] = "no-store"
        return response


class PartnerMoodIndexFeedView(View):
    """/partners/<slug>/feed/<token>/mood/<team_id>/ — индекс настроения клуба
    (агрегат без данных пользователей), активный сезон.
    """

    def get(self, request: HttpRequest, slug: str, token: str, team_id) -> HttpResponse:
        from seasons.models import Season
        from teams.models import Team

        partner = get_object_or_404(Partner, slug=slug, is_active=True)
        if str(partner.feed_token) != str(token):
            raise Http404()

        team = get_object_or_404(Team, pk=team_id)
        season = Season.get_primary_active()

        track_partner_feed_access(partner, request, feed_type="mood_index")
        data = build_mood_index_feed(partner, team, season)
        response = JsonResponse({"partner": partner.name, **data})
        response["Cache-Control"] = "no-store"
        return response


class PartnerReportView(View):
    """/partners/<slug>/report/<token>/ — отчёт рекламодателя по его баннерам. Доступ по feed_token."""

    def get(self, request: HttpRequest, slug: str, token: str) -> HttpResponse:
        from django.shortcuts import render

        from .selectors import banner_daily_series, stats_by_banner

        partner = get_object_or_404(Partner, slug=slug)
        if str(partner.feed_token) != str(token):
            raise Http404()
        try:
            days = min(max(int(request.GET.get("days", 30)), 7), 180)
        except ValueError:
            days = 30
        banners = list(partner.banners.all().order_by("-created_at"))
        period, total = stats_by_banner(days=days), stats_by_banner(days=None)
        empty = {"impressions": 0, "clicks": 0, "ctr_percent": 0.0}
        for b in banners:
            b.period = period.get(b.pk, empty)
            b.total = total.get(b.pk, empty)
        series = banner_daily_series([b.pk for b in banners], days=days)
        peak = max((d["impressions"] for d in series), default=0) or 1
        for d in series:
            d["pct"] = round(d["impressions"] * 100 / peak)
        impressions = sum(b.period["impressions"] for b in banners)
        clicks = sum(b.period["clicks"] for b in banners)
        response = render(request, "partners/report.html", {
            "partner": partner, "banners": banners, "series": series, "days": days,
            "impressions": impressions, "clicks": clicks,
            "ctr": round(clicks / impressions * 100, 2) if impressions else 0.0,
        })
        response["Cache-Control"] = "no-store"
        response["X-Robots-Tag"] = "noindex"
        return response
