# dashboard/admin_callback.py
"""UNFOLD["DASHBOARD_CALLBACK"]: данные для главной /admin/ из dashboard/services.py.
Ошибки не пробрасываются — /admin/ не должен падать.
"""
from __future__ import annotations

import logging

from . import services

logger = logging.getLogger(__name__)


def dashboard_callback(request, context):
    try:
        metrics = services.overview_metrics(days=30)
        health = services.data_health_summary(recent_runs=1)
        queue = services.antifraud_queue(limit=1)
        content = services.content_metrics(limit=8)

        context.update({
            "dopx_kpi": [
                {
                    "title": "Пользователей",
                    "metric": metrics["total_users"],
                    "footer": f"+{metrics['new_users_period']} за 30 дней",
                },
                {
                    "title": "Оценок",
                    "metric": metrics["total_evaluations"],
                    "footer": f"+{metrics['evaluations_period']} за 30 дней",
                },
                {
                    "title": "Live-матчи",
                    "metric": metrics["live_matches"],
                    "footer": f"{metrics['scheduled_matches']} запланировано",
                },
                {
                    "title": "Флагов в очереди",
                    "metric": queue["pending_flags_count"],
                    "footer": f"+{queue['pending_disputes_count']} диспутов",
                },
            ],
            "dopx_last_sync": health["last_run"],
            # Графики для Chart.js в templates/admin/index.html.
            "dopx_dau": metrics["dau"],
            "dopx_wau": metrics["wau"],
            "dopx_registrations_by_day": metrics["registrations_by_day"],
            "dopx_evaluations_by_day": metrics["evaluations_by_day"],
            # Контентные метрики.
            "dopx_top_matches": content["top_matches"],
            "dopx_top_players": content["top_players"],
            "dopx_rating_distribution": content["rating_distribution"],
            "dopx_matches_without_evaluations": content["matches_without_evaluations"],
        })
    except Exception:
        logger.error("dashboard_callback: не удалось посчитать KPI", exc_info=True)
    return context
