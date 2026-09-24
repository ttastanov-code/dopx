# partners/views.py
from __future__ import annotations

from django.conf import settings
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View

from core.utils import get_client_ip, is_rate_limited

from .models import Banner, Partner
from .services import (
    REFERRAL_COOKIE_MAX_AGE,
    REFERRAL_COOKIE_NAME,
    build_click_redirect_url,
    build_content_feed,
    build_mood_index_feed,
    track_banner_click,
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
            track_banner_click(banner, request)
        return redirect(build_click_redirect_url(banner))


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
