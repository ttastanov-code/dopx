# analytics/selectors.py
"""Selectors для founder-дэшборда. Результаты кэшируйте на уровне
вызывающей вьюхи (Redis, TTL 1ч) — сами селекторы кэш не трогают, чтобы
оставаться тестируемыми без моков."""
from __future__ import annotations

from collections import Counter
from datetime import timedelta
from typing import TypedDict

from django.db.models.functions import TruncDate, TruncWeek
from django.db.models import Count
from django.utils import timezone

from analytics.models import AnalyticsEvent, EventName


class FunnelStep(TypedDict):
    event_name: str
    unique_users: int


class ActivityPoint(TypedDict):
    period: str  # ISO date, начало дня/недели
    active_users: int


def registration_funnel(days: int = 30) -> list[FunnelStep]:
    """Воронка: регистрация → старт оценки → завершение, уникальные юзеры
    за `days` дней (не события — иначе активный юзер с 10 оценками
    искажает конверсию)."""
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
    """D7-retention: доля зарегистрированных в [cohort_start, cohort_end),
    выполнивших ЛЮБОЕ событие на 7-й день."""
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
    """Уникальные ЗАРЕГИСТРИРОВАННЫЕ пользователи с любым событием в этот
    день, по дням за последние `days`. Как и в `registration_funnel` —
    `user_id__isnull=False`: анонимный трафик (просмотры без логина) не
    входит в продуктовую метрику DAU, это отдельная (более шумная) метрика
    посещаемости, не активности вовлечённых пользователей.

    ВАЖНО: `Count('user_id', distinct=True)` считает уникальных юзеров
    ВНУТРИ каждого дня, а не подряд по всем дням — то есть сумма точек этого
    графика НЕ равна общему числу активных юзеров за период (один и тот же
    человек, активный 5 дней подряд, даёт 5 точек по 1), так и задумано для
    графика "сколько людей заходило В ЭТОТ день".
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
    """WAU по неделям (`TruncWeek` — начало ISO-недели, понедельник) за
    последние `weeks` недель. Та же оговорка про distinct ВНУТРИ периода,
    что и в `daily_active_users`."""
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
# Посещаемость и трафик (staff-дашборд, вкладка «Трафик») — продуктовый
# апгрейд "куча полезных метрик как у больших спортивных платформ".
# ============================================================

def _device_breakdown(user_agent_counts: dict[str, int]) -> dict:
    """Парсим КАЖДЫЙ уникальный user_agent из БД РОВНО ОДИН РАЗ (а не на
    каждую строку PAGE_VIEW — их могут быть сотни тысяч, а уникальных строк
    UA — единицы/десятки), затем умножаем результат парсинга на счётчик
    просмотров с этим UA. Ленивый импорт `user_agents` — тяжёлая библиотека
    с собственной regex-базой ua-parser, незачем грузить её на каждый
    импорт analytics/selectors.py, если вызывающий код траффик не смотрит."""
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
    """Посещаемость сайта — просмотры страниц, уникальные визиты, топ
    страниц/рефереров/UTM, устройства и браузеры. Источник данных —
    СУЩЕСТВУЮЩИЙ поток PAGE_VIEW из analytics/models.py (пишется на каждой
    загрузке страницы через static/js/analytics.js), отдельная система
    трекинга не нужна.

    "Уникальные визиты" считаем по `anonymous_id` (не по user_id) —
    комментарий в analytics/models.py явно фиксирует, что это единственное
    поле, которое переживает переход анонимный визит → регистрация → логин,
    то есть корректно отражает "один и тот же браузер", а не "один и тот же
    залогиненный юзер" (первое и есть определение "визита" в вебе).
    """
    since = timezone.now() - timedelta(days=days)
    page_views = AnalyticsEvent.objects.filter(event_name=EventName.PAGE_VIEW, created_at__gte=since)

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
