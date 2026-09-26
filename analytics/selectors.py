# analytics/selectors.py
"""Селекторы для аналитики дашборда. Кэш — на стороне вьюхи."""
from __future__ import annotations

from collections import Counter
from datetime import timedelta
from typing import TypedDict

from django.db.models.functions import TruncDate, TruncWeek
from django.db.models import Q, Count
from django.utils import timezone

from analytics.models import AnalyticsEvent, EventName


class FunnelStep(TypedDict):
    event_name: str
    unique_users: int


class ActivityPoint(TypedDict):
    period: str  # ISO date, начало дня/недели
    active_users: int


def registration_funnel(days: int = 30) -> list[FunnelStep]:
    """Воронка регистрация -> старт оценки -> завершение, по уникальным пользователям за days дней."""
    since = timezone.now() - timedelta(days=days)
    steps = [EventName.USER_REGISTERED, EventName.WIZARD_STARTED, EventName.EVALUATION_COMPLETED]
    return [
        {
            "event_name": step,
            "unique_users": AnalyticsEvent.objects.filter(
                event_name=step, created_at__gte=since, user_id__isnull=False
            ).values("user_id").distinct().count(),
        }
        for step in steps
    ]


def day7_retention(cohort_start, cohort_end) -> float:
    """D7-retention: доля зарегистрированных в когорте с любым событием на 7-й день."""
    user_ids = list(
        AnalyticsEvent.objects.filter(
            event_name=EventName.USER_REGISTERED,
            created_at__gte=cohort_start, created_at__lt=cohort_end, user_id__isnull=False,
        ).values_list("user_id", flat=True)
    )
    if not user_ids:
        return 0.0
    d7_start, d7_end = cohort_start + timedelta(days=7), cohort_end + timedelta(days=8)
    returned = AnalyticsEvent.objects.filter(
        user_id__in=user_ids, created_at__gte=d7_start, created_at__lt=d7_end,
    ).values("user_id").distinct().count()
    return round(returned / len(user_ids) * 100, 1)


def daily_active_users(days: int = 14) -> list[ActivityPoint]:
    """DAU: уникальные зарегистрированные пользователи с событием за день.
    Уникальность внутри дня — сумма точек не равна числу пользователей за период.
    """
    since = timezone.now() - timedelta(days=days)
    rows = (
        AnalyticsEvent.objects.filter(created_at__gte=since, user_id__isnull=False)
        .annotate(period=TruncDate("created_at"))
        .values("period")
        .annotate(active_users=Count("user_id", distinct=True))
        .order_by("period")
    )
    return [{"period": row["period"].isoformat(), "active_users": row["active_users"]} for row in rows]


def weekly_active_users(weeks: int = 12) -> list[ActivityPoint]:
    """WAU по неделям (с понедельника), уникальность внутри недели."""
    since = timezone.now() - timedelta(weeks=weeks)
    rows = (
        AnalyticsEvent.objects.filter(created_at__gte=since, user_id__isnull=False)
        .annotate(period=TruncWeek("created_at"))
        .values("period")
        .annotate(active_users=Count("user_id", distinct=True))
        .order_by("period")
    )
    return [{"period": row["period"].date().isoformat(), "active_users": row["active_users"]} for row in rows]


# ============================================================
# Посещаемость и трафик
# ============================================================

def _device_breakdown(user_agent_counts: dict[str, int]) -> dict:
    """Каждый уникальный user_agent парсим один раз и умножаем на счётчик просмотров.
    user_agents импортируется лениво.
    """
    from user_agents import parse as parse_ua

    device_buckets = {"desktop": 0, "mobile": 0, "tablet": 0, "bot": 0, "other": 0}
    browser_counter: Counter[str] = Counter()

    for ua_string, count in user_agent_counts.items():
        if not ua_string:
            device_buckets["other"] += count
            continue
        try:
            ua = parse_ua(ua_string)
        except Exception:
            device_buckets["other"] += count
            continue

        if ua.is_bot:
            device_buckets["bot"] += count
        elif ua.is_tablet:
            device_buckets["tablet"] += count
        elif ua.is_mobile:
            device_buckets["mobile"] += count
        elif ua.is_pc:
            device_buckets["desktop"] += count
        else:
            device_buckets["other"] += count

        browser_counter[ua.browser.family or "Другое"] += count

    top_browsers = [{"browser": name, "count": count} for name, count in browser_counter.most_common(6)]
    return {"devices": device_buckets, "top_browsers": top_browsers}


def traffic_overview(days: int = 14) -> dict:
    """Посещаемость по событиям PAGE_VIEW: просмотры, визиты, топ страниц/рефереров/UTM,
    устройства, браузеры. Визит — по anonymous_id.
    """
    since = timezone.now() - timedelta(days=days)
    # Служебные разделы (дашборд, админка) — не трафик аудитории.
    page_views = AnalyticsEvent.objects.filter(event_name=EventName.PAGE_VIEW, created_at__gte=since).exclude(
        Q(url_path__startswith="/staff/") | Q(url_path__startswith="/admin/")
    )

    total_page_views = page_views.count()
    unique_visitors = page_views.filter(anonymous_id__isnull=False).values("anonymous_id").distinct().count()

    pageviews_by_day = list(
        page_views.annotate(period=TruncDate("created_at"))
        .values("period").annotate(count=Count("id")).order_by("period")
    )
    top_pages = list(
        page_views.exclude(url_path="").values("url_path")
        .annotate(views=Count("id")).order_by("-views")[:10]
    )
    top_referrers = list(
        page_views.exclude(referrer="").values("referrer")
        .annotate(count=Count("id")).order_by("-count")[:10]
    )
    utm_sources = list(
        page_views.exclude(utm_source="").values("utm_source")
        .annotate(count=Count("id")).order_by("-count")[:10]
    )

    ua_counts = dict(
        page_views.exclude(user_agent="").values("user_agent")
        .annotate(count=Count("id")).values_list("user_agent", "count")
    )
    device_stats = _device_breakdown(ua_counts)

    return {
        "total_page_views": total_page_views,
        "unique_visitors": unique_visitors,
        "pageviews_by_day": [
            {"period": row["period"].isoformat(), "count": row["count"]} for row in pageviews_by_day
        ],
        "top_pages": top_pages,
        "top_referrers": top_referrers,
        "utm_sources": utm_sources,
        "devices": device_stats["devices"],
        "top_browsers": device_stats["top_browsers"],
    }
