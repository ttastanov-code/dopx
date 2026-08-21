# partners/selectors.py
"""Read-only агрегаты по партнёрской активности поверх AnalyticsEvent.properties (JSONField)."""
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
    """{'impressions': N, 'clicks': N, 'ctr_percent': N} за последние `days` дней."""
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
    """
    Топ сущностей конкретного типа виджета (player/team) по числу просмотров
    embed'а за N дней — группировка по properties__entity_id (JSONField).
    Раньше widget_embed_views умел посчитать только ОДНУ уже известную
    сущность (нужно заранее знать player_id/team_id) — для staff-страницы
    "какие виджеты вообще встраивают" нужен обратный запрос: дай топ сам,
    без входного ID. Возвращает [{'entity_id': str, 'views': int}, ...],
    отсортировано по views убыв. Разрешение entity_id → реальный объект
    (Player/Team) — на вызывающей стороне (dashboard/views.py), сюда
    намеренно не тащим импорт players/teams моделей — этот модуль работает
    только поверх AnalyticsEvent.
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
    """{'player': N, 'team': N, 'standings': N} — общее число просмотров
    embed-виджетов каждого типа за N дней, для сводки на staff-странице."""
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
# Ниже — то же самое, что banner_stats/partner_referral_visits выше, но
# АГРЕГИРОВАННОЕ по всем баннерам/партнёрам сразу (а не по одному id на
# вызов) — раньше эти суммарные цифры нигде не считались: в Django admin
# показывалась статистика только конкретного баннера/партнёра в своей же
# строке таблицы, без общей картины "сколько показов/кликов/визитов у нас
# по рекламе за месяц в целом". Нужно для staff-страницы "Реклама и виджеты"
# (dashboard/views.py::ads_view), куда объединили статистику баннеров,
# партнёрских рефералок и embed-виджетов.
# ============================================================

def banner_totals(*, days: int = DEFAULT_WINDOW_DAYS) -> dict:
    """{'impressions': N, 'clicks': N, 'ctr_percent': N} — сумма по ВСЕМ баннерам за N дней."""
    since = _since(days)
    impressions = AnalyticsEvent.objects.filter(event_name=EventName.BANNER_IMPRESSION, created_at__gte=since).count()
    clicks = AnalyticsEvent.objects.filter(event_name=EventName.BANNER_CLICK, created_at__gte=since).count()
    ctr = round(clicks / impressions * 100, 2) if impressions else 0.0
    return {"impressions": impressions, "clicks": clicks, "ctr_percent": ctr}


def top_banners(*, days: int = DEFAULT_WINDOW_DAYS, limit: int = 10) -> list[dict]:
    """
    Топ баннеров по показам за N дней — группировка по properties__banner_id,
    клики досчитываются вторым запросом по тем же id (два GROUP BY дешевле,
    чем JOIN показов и кликов внутри JSONField). Возвращает
    [{'banner_id': str, 'impressions': int, 'clicks': int, 'ctr_percent': float}, ...],
    отсортировано по показам убыв. Разрешение banner_id → объект Banner —
    на вызывающей стороне, тот же паттерн, что и top_widget_entities.
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
    """Сумма визитов по реферальным ссылкам ВСЕХ партнёров за N дней."""
    return AnalyticsEvent.objects.filter(
        event_name=EventName.PARTNER_REFERRAL_VISIT, created_at__gte=_since(days),
    ).count()


def top_partners_by_referral_visits(*, days: int = DEFAULT_WINDOW_DAYS, limit: int = 10) -> list[dict]:
    """Топ партнёров по визитам с их реферальной ссылки за N дней.
    Возвращает [{'partner_slug': str, 'visits': int}, ...]."""
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
