# dashboard/views/overview.py
"""Обзор, трафик, удержание."""
from __future__ import annotations

from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import render

from users.city_stats import dashboard_city_breakdown

from .. import services


# Пресеты периода для обзора (вьюха + кнопки в шаблоне).
OVERVIEW_DAY_PRESETS = [7, 14, 30, 90]


@staff_member_required
def overview(request):
    try:
        days = int(request.GET.get("days", 14))
    except (TypeError, ValueError):
        days = 14
    days = days if days in OVERVIEW_DAY_PRESETS else 14

    context = {
        "page_title": "Обзор — DOPX Staff",
        "active_tab": "overview",
        "metrics": services.overview_metrics(days=days),
        "attention": services.attention_items(request.user),
        "cities": dashboard_city_breakdown(days=days),
        "selected_days": days,
        "day_presets": OVERVIEW_DAY_PRESETS,
    }
    return render(request, "dashboard/overview.html", context)


@staff_member_required
def traffic(request):
    try:
        days = int(request.GET.get("days", 14))
    except (TypeError, ValueError):
        days = 14
    days = days if days in OVERVIEW_DAY_PRESETS else 14

    context = {
        "page_title": "Трафик — DOPX Staff",
        "active_tab": "traffic",
        "traffic": services.traffic_summary(days=days),
        "selected_days": days,
        "day_presets": OVERVIEW_DAY_PRESETS,
    }
    return render(request, "dashboard/traffic.html", context)


@staff_member_required
def retention(request):
    """Воронка «визит → регистрация → действие» и недельные когорты удержания."""
    from analytics.selectors import funnel_overview, retention_cohorts

    try:
        days = int(request.GET.get("days", 30))
    except (TypeError, ValueError):
        days = 30
    days = days if days in (7, 14, 30, 90) else 30
    return render(request, "dashboard/retention.html", {
        "page_title": "Воронка и удержание — DOPX Staff",
        "active_tab": "traffic",
        "funnel": funnel_overview(days=days),
        "cohorts": retention_cohorts(weeks=8),
        "week_numbers": range(8),
        "selected_days": days,
        "day_presets": (7, 14, 30, 90),
    })
