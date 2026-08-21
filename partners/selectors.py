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
