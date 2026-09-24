# partners/selectors.py
"""Агрегаты по партнёрской активности поверх AnalyticsEvent.properties."""
from __future__ import annotations

from datetime import timedelta

from django.db.models import Count
from django.utils import timezone

from analytics.models import AnalyticsEvent, EventName

DEFAULT_WINDOW_DAYS = 30


def _since(days: int = DEFAULT_WINDOW_DAYS):
    return timezone.now() - timedelta(days=days)


def partner_referral_visits(partner_slug: str, *, days: int = DEFAULT_WINDOW_DAYS) -> int:
    return AnalyticsEvent.objects.filter(
        event_name=EventName.PARTNER_REFERRAL_VISIT,
        properties__partner_slug=partner_slug,
        created_at__gte=_since(days),
    ).count()


def banner_stats(banner_id: str, *, days: int = DEFAULT_WINDOW_DAYS) -> dict:
    """{'impressions', 'clicks', 'ctr_percent'} за days дней."""
    since = _since(days)
    impressions = AnalyticsEvent.objects.filter(
        event_name=EventName.BANNER_IMPRESSION,
        properties__banner_id=str(banner_id),
        created_at__gte=since,
    ).count()
    clicks = AnalyticsEvent.objects.filter(
        event_name=EventName.BANNER_CLICK,
        properties__banner_id=str(banner_id),
        created_at__gte=since,
    ).count()
    ctr = round(clicks / impressions * 100, 2) if impressions else 0.0
    return {"impressions": impressions, "clicks": clicks, "ctr_percent": ctr}


def widget_embed_views(widget_type: str, entity_id: str, *, days: int = DEFAULT_WINDOW_DAYS) -> int:
    return AnalyticsEvent.objects.filter(
        event_name=EventName.WIDGET_EMBED_VIEWED,
        properties__widget_type=widget_type,
        properties__entity_id=str(entity_id),
        created_at__gte=_since(days),
    ).count()


def top_widget_entities(widget_type: str, *, days: int = DEFAULT_WINDOW_DAYS, limit: int = 10) -> list[dict]:
    """Топ сущностей виджета (player/team) по просмотрам за N дней.
    [{'entity_id': str, 'views': int}, ...]; объекты резолвит вызывающий код.
    """
    rows = (
        AnalyticsEvent.objects.filter(
            event_name=EventName.WIDGET_EMBED_VIEWED,
            properties__widget_type=widget_type,
            created_at__gte=_since(days),
        )
        .values("properties__entity_id")
        .annotate(views=Count("id"))
        .order_by("-views")[:limit]
    )
    return [
        {"entity_id": row["properties__entity_id"], "views": row["views"]}
        for row in rows
        if row["properties__entity_id"]
    ]


def widget_embed_totals(*, days: int = DEFAULT_WINDOW_DAYS) -> dict:
    """{'player': N, 'team': N, 'standings': N} — просмотры виджетов по типам."""
    since = _since(days)
    rows = (
        AnalyticsEvent.objects.filter(event_name=EventName.WIDGET_EMBED_VIEWED, created_at__gte=since)
        .values("properties__widget_type")
        .annotate(views=Count("id"))
    )
    totals = {"player": 0, "team": 0, "standings": 0}
    for row in rows:
        wtype = row["properties__widget_type"]
        if wtype in totals:
            totals[wtype] = row["views"]
    return totals


# ============================================================
# Суммарные цифры по всем баннерам/партнёрам (страница «Реклама и виджеты»)
# ============================================================

def banner_totals(*, days: int = DEFAULT_WINDOW_DAYS) -> dict:
    """Сумма показов/кликов/CTR по всем баннерам."""
    since = _since(days)
    impressions = AnalyticsEvent.objects.filter(event_name=EventName.BANNER_IMPRESSION, created_at__gte=since).count()
    clicks = AnalyticsEvent.objects.filter(event_name=EventName.BANNER_CLICK, created_at__gte=since).count()
    ctr = round(clicks / impressions * 100, 2) if impressions else 0.0
    return {"impressions": impressions, "clicks": clicks, "ctr_percent": ctr}


def top_banners(*, days: int = DEFAULT_WINDOW_DAYS, limit: int = 10) -> list[dict]:
    """Топ баннеров по показам; клики — вторым запросом.
    [{'banner_id', 'impressions', 'clicks', 'ctr_percent'}, ...].
    """
    since = _since(days)
    impression_rows = (
        AnalyticsEvent.objects.filter(event_name=EventName.BANNER_IMPRESSION, created_at__gte=since)
        .values("properties__banner_id")
        .annotate(impressions=Count("id"))
        .order_by("-impressions")[:limit]
    )
    banner_ids = [row["properties__banner_id"] for row in impression_rows if row["properties__banner_id"]]
    click_counts = dict(
        AnalyticsEvent.objects.filter(
            event_name=EventName.BANNER_CLICK, created_at__gte=since,
            properties__banner_id__in=banner_ids,
        )
        .values("properties__banner_id")
        .annotate(clicks=Count("id"))
        .values_list("properties__banner_id", "clicks")
    )
    result = []
    for row in impression_rows:
        banner_id = row["properties__banner_id"]
        if not banner_id:
            continue
        impressions = row["impressions"]
        clicks = click_counts.get(banner_id, 0)
        result.append({
            "banner_id": banner_id,
            "impressions": impressions,
            "clicks": clicks,
            "ctr_percent": round(clicks / impressions * 100, 2) if impressions else 0.0,
        })
    return result


def partner_referral_totals(*, days: int = DEFAULT_WINDOW_DAYS) -> int:
    """Сумма визитов по реферальным ссылкам."""
    return AnalyticsEvent.objects.filter(
        event_name=EventName.PARTNER_REFERRAL_VISIT, created_at__gte=_since(days),
    ).count()


def top_partners_by_referral_visits(*, days: int = DEFAULT_WINDOW_DAYS, limit: int = 10) -> list[dict]:
    """Топ партнёров по визитам: [{'partner_slug', 'visits'}, ...]."""
    rows = (
        AnalyticsEvent.objects.filter(event_name=EventName.PARTNER_REFERRAL_VISIT, created_at__gte=_since(days))
        .values("properties__partner_slug")
        .annotate(visits=Count("id"))
        .order_by("-visits")[:limit]
    )
    return [
        {"partner_slug": row["properties__partner_slug"], "visits": row["visits"]}
        for row in rows
        if row["properties__partner_slug"]
    ]
