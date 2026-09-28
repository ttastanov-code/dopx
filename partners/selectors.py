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


def _ctr(clicks: int, impressions: int) -> float:
    return round(clicks / impressions * 100, 2) if impressions else 0.0


def banner_stats(banner_id, *, days: int | None = DEFAULT_WINDOW_DAYS) -> dict:
    """{'impressions', 'clicks', 'ctr_percent'} за days дней (None — за всё время)."""
    from django.db.models import Sum

    from .models import BannerDailyStat

    qs = BannerDailyStat.objects.filter(banner_id=banner_id)
    if days:
        qs = qs.filter(date__gte=_since(days).date())
    row = qs.aggregate(i=Sum("impressions"), c=Sum("clicks"))
    impressions, clicks = row["i"] or 0, row["c"] or 0
    return {"impressions": impressions, "clicks": clicks, "ctr_percent": _ctr(clicks, impressions)}


def banner_daily_series(banner_ids, *, days: int = DEFAULT_WINDOW_DAYS) -> list[dict]:
    """[{date, impressions, clicks}] по дням за период, пропуски — нулями."""
    from django.db.models import Sum
    from django.utils import timezone

    from .models import BannerDailyStat

    today = timezone.localdate()
    start = today - timedelta(days=days - 1)
    rows = {
        r["date"]: r for r in BannerDailyStat.objects.filter(banner_id__in=list(banner_ids), date__gte=start)
        .values("date").annotate(impressions=Sum("impressions"), clicks=Sum("clicks"))
    }
    series = []
    for i in range(days):
        d = start + timedelta(days=i)
        r = rows.get(d, {})
        series.append({"date": d, "impressions": r.get("impressions", 0), "clicks": r.get("clicks", 0)})
    return series


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
    from django.db.models import Sum

    from .models import BannerDailyStat

    row = BannerDailyStat.objects.filter(date__gte=_since(days).date()).aggregate(i=Sum("impressions"), c=Sum("clicks"))
    impressions, clicks = row["i"] or 0, row["c"] or 0
    return {"impressions": impressions, "clicks": clicks, "ctr_percent": _ctr(clicks, impressions)}


def stats_by_banner(*, days: int | None = DEFAULT_WINDOW_DAYS) -> dict:
    """{banner_id: {'impressions', 'clicks', 'ctr_percent'}} одним запросом."""
    from django.db.models import Sum

    from .models import BannerDailyStat

    qs = BannerDailyStat.objects.all()
    if days:
        qs = qs.filter(date__gte=_since(days).date())
    return {
        r["banner_id"]: {"impressions": r["i"], "clicks": r["c"], "ctr_percent": _ctr(r["c"], r["i"])}
        for r in qs.values("banner_id").annotate(i=Sum("impressions"), c=Sum("clicks"))
    }


def top_banners(*, days: int = DEFAULT_WINDOW_DAYS, limit: int = 10) -> list[dict]:
    """Топ баннеров по показам: [{'banner_id', 'impressions', 'clicks', 'ctr_percent'}, ...]."""
    stats = stats_by_banner(days=days)
    ranked = sorted(stats.items(), key=lambda kv: -kv[1]["impressions"])[:limit]
    return [{"banner_id": str(bid), **row} for bid, row in ranked]


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
