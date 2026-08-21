# partners/services.py
"""
Сервисный слой партнёрской инфраструктуры: выбор баннера для показа,
трекинг impression/click/referral-визита через analytics.track_event
(см. partners/models.py — почему не отдельные таблицы).
"""
from __future__ import annotations

import random
from urllib.parse import urlencode, urlparse, urlunparse, parse_qsl

from django.http import HttpRequest

from analytics.models import EventName
from analytics.services import track_event

from .models import Banner, BannerZone, Partner

# Имя cookie реферальной атрибуции — читается в users/views.py при
# регистрации, чтобы привязать USER_REGISTERED к партнёру, приведшему
# визит, а не только сам факт визита по /go/<slug>/.
REFERRAL_COOKIE_NAME = "dopx_ref"
REFERRAL_COOKIE_MAX_AGE = 60 * 60 * 24 * 30  # 30 дней


def get_active_banner_for_zone(zone: str) -> Banner | None:
    """
    Взвешенный случайный выбор одного активного баннера зоны. Вес —
    priority + 1 (баннер с priority=0 всё равно участвует в розыгрыше,
    просто с наименьшим весом, а не исключается).
    """
    candidates = list(
        Banner.objects.filter(zone=zone, is_active=True).select_related("partner")
    )
    candidates = [b for b in candidates if b.is_currently_active()]
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    weights = [b.priority + 1 for b in candidates]
    return random.choices(candidates, weights=weights, k=1)[0]


def _banner_properties(banner: Banner) -> dict:
    return {
        "banner_id": str(banner.id),
        "zone": banner.zone,
        "partner_slug": banner.partner.slug if banner.partner_id else None,
    }


def track_banner_impression(banner: Banner, request: HttpRequest) -> None:
    track_event(EventName.BANNER_IMPRESSION, request=request, properties=_banner_properties(banner))


def track_banner_click(banner: Banner, request: HttpRequest) -> None:
    track_event(EventName.BANNER_CLICK, request=request, properties=_banner_properties(banner))


def build_click_redirect_url(banner: Banner) -> str:
    """
    target_url партнёра + utm_source/utm_medium/utm_campaign, если их там
    ещё нет — чтобы партнёр в СВОЕЙ аналитике тоже видел трафик от DOPX
    (двусторонняя атрибуция, а не только наша). Не перетирает существующие
    utm-параметры, если рекламодатель уже проставил свои.
    """
    parsed = urlparse(banner.target_url)
    query_params = dict(parse_qsl(parsed.query))
    query_params.setdefault("utm_source", "dopx")
    query_params.setdefault("utm_medium", "banner")
    query_params.setdefault("utm_campaign", banner.zone)
    return urlunparse(parsed._replace(query=urlencode(query_params)))


def track_partner_referral_visit(partner: Partner, request: HttpRequest, *, next_path: str = "") -> None:
    track_event(
        EventName.PARTNER_REFERRAL_VISIT,
        request=request,
        properties={"partner_slug": partner.slug, "next": next_path[:200]},
    )


def build_content_feed(partner: Partner, request: HttpRequest, *, limit: int = 10) -> list[dict]:
    """
    Закрытый контент-фид (partners/views.py::PartnerContentFeedView) — НЕ
    публичный API поверх спарсенных у KFF данных, а готовые брендированные
    ассеты под последние завершённые матчи: ссылка на PNG-карточку матча
    (core/views.py::MatchShareCardView, уже с логотипом DOPX и подписью
    dopx.kz) + подпись с НАШЕЙ аналитикой (drama_index, итоговый счёт), а не
    сырые оценки/статистика. Партнёр получает контент для своего канала на
    каждый тур, не доступ к базе.
    """
    from django.urls import reverse

    from matches.models import Match

    matches = (
        Match.objects.filter(status="finished")
        .select_related("home_team", "away_team", "aggregate")
        .order_by("-start_time")[:limit]
    )

    items = []
    for match in matches:
        match_url = request.build_absolute_uri(reverse("matches:detail", args=[match.id]))
        card_url = request.build_absolute_uri(reverse("core:match_share_card", args=[match.id]))
        drama_index = getattr(getattr(match, "aggregate", None), "drama_index", None)
        caption = f"{match.home_team.name} {match.home_score}:{match.away_score} {match.away_team.name}"
        if drama_index:
            caption += f" — индекс драмы {drama_index:.1f}/10 по мнению болельщиков DOPX"
        items.append({
            "match_id": str(match.id),
            "match_url": match_url,
            "image_url": card_url,
            "caption": caption,
            "date": match.start_time.isoformat(),
        })
    return items


def track_partner_feed_access(partner: Partner, request: HttpRequest) -> None:
    track_event(
        EventName.PARTNER_FEED_ACCESSED, request=request,
        properties={"partner_slug": partner.slug},
    )


def track_widget_embed_view(*, widget_type: str, entity_id: str, request: HttpRequest) -> None:
    """
    Трекинг открытия embed-виджета. `widget_type` — 'player'/'team'/'standings'
    (players/views.py::player_rating_widget, teams/views.py::team_rating_widget,
    core/views.py::standings_widget). HTTP_REFERER на iframe-запросе — это
    URL страницы, которая ЕГО встроила (домен встраивающего партнёра), а не
    наша страница — именно этого не хватало до продуктового аудита "канал
    привлечения" (2026-08-21), чтобы вообще узнать, кто и где использует виджет.
    """
    track_event(
        EventName.WIDGET_EMBED_VIEWED,
        request=request,
        properties={
            "widget_type": widget_type,
            "entity_id": entity_id,
            "embedder_referrer": request.META.get("HTTP_REFERER", "")[:500],
        },
    )
