# partners/services.py
"""Партнёры: выбор баннера, трекинг показов/кликов/переходов через analytics.track_event."""
from __future__ import annotations

import random
from urllib.parse import urlencode, urlparse, urlunparse, parse_qsl

from django.http import HttpRequest

from analytics.models import EventName
from analytics.services import track_event

from .models import Banner, BannerZone, Partner

# Cookie реферальной атрибуции (читается при регистрации).
REFERRAL_COOKIE_NAME = "dopx_ref"
REFERRAL_COOKIE_MAX_AGE = 60 * 60 * 24 * 30  # 30 дней


def get_active_banner_for_zone(zone: str) -> Banner | None:
    """Взвешенный случайный выбор активного баннера зоны (вес = priority + 1)."""
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
    """target_url + utm-метки, если их там ещё нет."""
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
    """Контент-фид: последние завершённые матчи — ссылка на PNG-карточку + подпись с нашей аналитикой."""
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
            # drama_index — шкала 0..100.
            caption += f" — индекс драмы {drama_index:.0f}/100 по мнению болельщиков DOPX"
        items.append({
            "match_id": str(match.id),
            "match_url": match_url,
            "image_url": card_url,
            "caption": caption,
            "date": match.start_time.isoformat(),
        })
    return items


def track_partner_feed_access(partner: Partner, request: HttpRequest, *, feed_type: str = "content") -> None:
    """:param feed_type: 'content' или 'mood_index' — одно событие, различаем по properties."""
    track_event(
        EventName.PARTNER_FEED_ACCESSED, request=request,
        properties={"partner_slug": partner.slug, "feed_type": feed_type},
    )


def build_mood_index_feed(partner: Partner, team, season) -> dict:
    """Индекс настроения клуба для партнёра — тот же агрегат, что на странице команды,
    без данных пользователей.
    """
    from teams.services import build_sparkline_points, compute_mood_series, find_season_controversial_matches

    series = compute_mood_series(team)
    controversial = find_season_controversial_matches(team, season) if season else []
    return {
        "team": team.name,
        "season": season.year if season else None,
        "mood_series": [
            {
                "date": point["label"],
                "opponent": point["opponent"],
                "trust": point["trust"],
                "mood": point["mood"],
                "expectation_pct": point["expectation_pct"],
            }
            for point in series
        ],
        "trust_sparkline_points": build_sparkline_points(series, "trust", value_max=10.0),
        "mood_sparkline_points": build_sparkline_points(series, "mood", value_max=10.0),
        "controversial_matches": [
            {"label": item["label"], "gap": item["gap"], "date": item["date"].isoformat()}
            for item in controversial
        ],
    }


def track_widget_embed_view(*, widget_type: str, entity_id: str, request: HttpRequest) -> None:
    """Трекинг открытия embed-виджета. widget_type — 'player'/'team'/'standings',
    HTTP_REFERER — страница, где встроен виджет.
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
