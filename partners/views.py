# partners/views.py
from __future__ import annotations

from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View

from .models import Banner, Partner
from .services import (
    REFERRAL_COOKIE_MAX_AGE,
    REFERRAL_COOKIE_NAME,
    build_click_redirect_url,
    build_content_feed,
    track_banner_click,
    track_partner_feed_access,
    track_partner_referral_visit,
)


class PartnerReferralRedirectView(View):
    """
    /go/<slug>/ — реферальная ссылка для партнёра. Логирует визит
    (partners/services.py::track_partner_referral_visit) и кладёт cookie
    атрибуции на REFERRAL_COOKIE_MAX_AGE — users/views.py::RegisterView
    читает её при регистрации, чтобы привязать сам факт регистрации
    к партнёру, а не только визит.

    ?next=/some/path — куда редиректить после атрибуции (по умолчанию —
    главная). Валидируется через url_has_allowed_host_and_scheme, чтобы
    партнёрская ссылка не превратилась в open redirect.
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

        track_partner_referral_visit(partner, request, next_path=redirect_to)

        response = redirect(redirect_to)
        response.set_cookie(
            REFERRAL_COOKIE_NAME,
            partner.slug,
            max_age=REFERRAL_COOKIE_MAX_AGE,
            samesite="Lax",
        )
        return response


class BannerClickRedirectView(View):
    """
    /ad/<uuid:pk>/click/ — все клики по баннерам идут через этот роут
    вместо прямой ссылки на target_url, иначе клик невозможно посчитать
    (partners/selectors.py::banner_stats нужен для любого разговора с
    партнёром про эффективность размещения).
    """

    def get(self, request: HttpRequest, pk) -> HttpResponse:
        banner = get_object_or_404(Banner, pk=pk)
        track_banner_click(banner, request)
        return redirect(build_click_redirect_url(banner))


class PartnerContentFeedView(View):
    """
    /partners/<slug>/feed/<token>/ — закрытый JSON-фид готовых брендированных
    ассетов под последние матчи (partners/services.py::build_content_feed).
    Токен — Partner.feed_token (UUID, генерируется автоматически, отдельно
    от публичного slug), а не Basic Auth/API-ключ в заголовке: партнёр без
    техотдела может просто дать эту ссылку своему SMM-редактору или
    подключить её как источник в Zapier/Make без настройки авторизации.
    """

    def get(self, request: HttpRequest, slug: str, token: str) -> HttpResponse:
        partner = get_object_or_404(Partner, slug=slug, is_active=True)
        if str(partner.feed_token) != str(token):
            # 404, а не 403 — не подтверждаем существование партнёра с этим
            # slug тому, кто подбирает токен наугад.
            raise Http404()

        track_partner_feed_access(partner, request)
        items = build_content_feed(partner, request)
        return JsonResponse({"partner": partner.name, "items": items})
