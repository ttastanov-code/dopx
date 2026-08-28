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
    track_banner_click,
    track_partner_feed_access,
    track_partner_referral_visit,
)

# Лимит на накрутку статистики переходов/кликов скриптом с одного IP по
# одному slug/uuid — см. докстринги вьюх ниже. Redirect остаётся плавным
# для пользователя в любом случае (не 429) — лимитируется только запись в
# статистику (core/utils.py::is_rate_limited, тот же паттерн, что
# users/views.py::RegisterView/VerifyEmailView).
PARTNER_STATS_RATE_LIMIT = 30
PARTNER_STATS_RATE_LIMIT_WINDOW_SECONDS = 60


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

        # БАГ, КОТОРЫЙ ТУТ БЫЛ: без rate-limit скрипт мог долбить /go/<slug>/
        # в цикле и накручивать track_partner_referral_visit — статистика
        # партнёра (переходы) становилась недостоверной. Лимитируем именно
        # запись в статистику, а не сам редирект: пользователь ничего не
        # замечает (нет 429/ошибки), просто повторные визиты сверх лимита с
        # одного IP по этому же slug не засчитываются как "новые".
        client_ip = get_client_ip(request)
        if not client_ip or not is_rate_limited(
            f'partner_referral:{partner.slug}:{client_ip}',
            PARTNER_STATS_RATE_LIMIT, PARTNER_STATS_RATE_LIMIT_WINDOW_SECONDS,
        ):
            track_partner_referral_visit(partner, request, next_path=redirect_to)

        response = redirect(redirect_to)
        # БАГ, КОТОРЫЙ ТУТ БЫЛ: cookie ставилась НЕподписанной (set_cookie) —
        # любой мог вручную выставить себе `dopx_ref=<slug другого партнёра>`
        # в браузере (или скриптом, минуя /go/<slug>/ вообще) и приписать
        # свою регистрацию произвольному партнёру в обход track_partner_
        # referral_visit — попадание в комиссионные/отчётность партнёра без
        # реального перехода по его ссылке. set_signed_cookie подписывает
        # значение секретом Django (SECRET_KEY + salt) — подделать его без
        # знания секрета нельзя; users/views.py::RegisterView.form_valid
        # соответственно читает её через get_signed_cookie (см. там же).
        response.set_signed_cookie(
            REFERRAL_COOKIE_NAME,
            partner.slug,
            salt="partners.referral",
            max_age=REFERRAL_COOKIE_MAX_AGE,
            samesite="Lax",
            # httponly — эта cookie нужна только серверу (users/views.py::
            # RegisterView читает её при регистрации), фронтенду её
            # содержимое никогда не требуется читать через JS, значит нет
            # причины оставлять её доступной для XSS. secure — тот же
            # паттерн, что и SESSION_COOKIE_SECURE/CSRF_COOKIE_SECURE в
            # settings.py (not DEBUG, а не жёсткий True, иначе cookie не
            # ставится на локальном http-сервере разработки).
            httponly=True,
            secure=not settings.DEBUG,
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
        # Тот же принцип, что в PartnerReferralRedirectView выше: лимит на
        # накрутку клика по счётчику, редирект на target_url всегда плавный.
        client_ip = get_client_ip(request)
        if not client_ip or not is_rate_limited(
            f'banner_click:{banner.pk}:{client_ip}',
            PARTNER_STATS_RATE_LIMIT, PARTNER_STATS_RATE_LIMIT_WINDOW_SECONDS,
        ):
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
        response = JsonResponse({"partner": partner.name, "items": items})
        # Токен доступа — часть URL (partners/services.py::build_content_feed
        # докстринг объясняет, почему так удобнее партнёру). Минус — ссылка с
        # токеном может осесть в истории браузера, логах прокси/CDN на
        # СТОРОНЕ партнёра, системах веб-аналитики. no-store запрещает
        # кэширование ответа где бы то ни было по цепочке — снижает шанс,
        # что содержимое (пусть и не сверхсекретное) утечёт через чужой кэш.
        response["Cache-Control"] = "no-store"
        return response
