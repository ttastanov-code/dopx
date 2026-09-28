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


TOTALS_CACHE_TTL = 60
FREQ_SESSION_KEY = "ad_views"


def banner_totals_cached(banner_id) -> dict:
    """Показы и клики за всё время (для лимитов), кэш на минуту."""
    from django.core.cache import cache
    from django.db.models import Sum

    from .models import BannerDailyStat

    key = f"ad:totals:{banner_id}"
    totals = cache.get(key)
    if totals is None:
        row = BannerDailyStat.objects.filter(banner_id=banner_id).aggregate(i=Sum("impressions"), c=Sum("clicks"))
        totals = {"impressions": row["i"] or 0, "clicks": row["c"] or 0}
        cache.set(key, totals, TOTALS_CACHE_TTL)
    return totals


def _views_today(request) -> dict:
    """{banner_id: показов сегодня} этому посетителю — из сессии."""
    from django.utils import timezone

    if request is None or not hasattr(request, "session"):
        return {}
    data = request.session.get(FREQ_SESSION_KEY) or {}
    if data.get("date") != timezone.localdate().isoformat():
        return {}
    return data.get("views", {})


def pick_banner(zone: str, request=None) -> Banner | None:
    """Баннер для зоны: запущен, подходит по аудитории, не упёрся в лимиты. Ротация по весу."""
    user = getattr(request, "user", None)
    is_user = bool(user and user.is_authenticated)
    views = _views_today(request)
    candidates = []
    for banner in Banner.objects.filter(zone=zone, is_active=True).select_related("partner"):
        if banner.audience == "guests" and is_user or banner.audience == "users" and not is_user:
            continue
        needs_totals = banner.max_impressions or banner.max_clicks
        if banner.status(banner_totals_cached(banner.pk) if needs_totals else None) != "running":
            continue
        if banner.daily_cap_per_visitor and views.get(str(banner.pk), 0) >= banner.daily_cap_per_visitor:
            continue
        candidates.append(banner)
    if not candidates:
        return None
    return random.choices(candidates, weights=[max(b.priority, 1) for b in candidates], k=1)[0]


def _bump(banner_id, field: str) -> None:
    from django.core.cache import cache
    from django.db.models import F
    from django.utils import timezone

    from .models import BannerDailyStat

    today = timezone.localdate()
    updated = BannerDailyStat.objects.filter(banner_id=banner_id, date=today).update(**{field: F(field) + 1})
    if not updated:
        stat, created = BannerDailyStat.objects.get_or_create(banner_id=banner_id, date=today, defaults={field: 1})
        if not created:
            BannerDailyStat.objects.filter(pk=stat.pk).update(**{field: F(field) + 1})
    cache.delete(f"ad:totals:{banner_id}")


def record_impression(banner: Banner, request) -> None:
    """Баннер был виден на экране: +1 показ и счётчик частоты посетителя."""
    from django.utils import timezone

    _bump(banner.pk, "impressions")
    if hasattr(request, "session"):
        today = timezone.localdate().isoformat()
        data = request.session.get(FREQ_SESSION_KEY) or {}
        if data.get("date") != today:
            data = {"date": today, "views": {}}
        data["views"][str(banner.pk)] = data["views"].get(str(banner.pk), 0) + 1
        request.session[FREQ_SESSION_KEY] = data


def record_click(banner: Banner, request) -> None:
    _bump(banner.pk, "clicks")
    track_event(EventName.BANNER_CLICK, request=request, properties=_banner_properties(banner))


def _banner_properties(banner: Banner) -> dict:
    return {
        "banner_id": str(banner.id),
        "zone": banner.zone,
        "partner_slug": banner.partner.slug if banner.partner_id else None,
    }


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


def is_external_embed(request: HttpRequest) -> bool:
    """Виджет открыт на чужом сайте: Referer есть и его домен не наш.
    Превью на наших страницах (карточка игрока, дашборд) встраиванием не считаются."""
    from django.conf import settings

    referrer = request.META.get("HTTP_REFERER", "")
    host = (urlparse(referrer).hostname or "").lower()
    if not host:
        return False
    own = {(request.get_host() or "").split(":")[0].lower(), "localhost", "127.0.0.1"}
    site_host = urlparse(getattr(settings, "SITE_URL", "") or "").hostname
    if site_host:
        own.add(site_host.lower())
    return host not in own


def track_widget_embed_view(*, widget_type: str, entity_id: str, request: HttpRequest) -> None:
    """Встраивание виджета на чужом сайте. widget_type — 'player'/'team'/'standings'/..."""
    if not is_external_embed(request):
        return
    track_event(
        EventName.WIDGET_EMBED_VIEWED,
        request=request,
        properties={
            "widget_type": widget_type,
            "entity_id": entity_id,
            "embedder_referrer": request.META.get("HTTP_REFERER", "")[:500],
        },
    )
