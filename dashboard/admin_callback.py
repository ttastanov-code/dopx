# dashboard/admin_callback.py
"""
UNFOLD["DASHBOARD_CALLBACK"] (см. dopx/settings.py) — Unfold вызывает эту
функцию при рендере /admin/ (главная страница) и передаёт результат в
контекст шаблона. Переиспользуем те же агрегаты, что и /staff/dashboard/
overview (dashboard/services.py) — одна и та же цифра "верификация email"
не должна тихо разъезжаться между двумя разными способами её посчитать.

Контракт Unfold: callback(request, context) -> dict (context с добавками).
Намеренно НЕ бросаем исключения наружу — если что-то в агрегатах сломается,
это не должно ронять /admin/ целиком (страница входа в систему буквально).
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
            # Графики (те же данные, что на /staff/dashboard/) — рендерятся
            # Chart.js прямо в templates/admin/index.html через json_script.
            "dopx_dau": metrics["dau"],
            "dopx_wau": metrics["wau"],
            "dopx_registrations_by_day": metrics["registrations_by_day"],
            "dopx_evaluations_by_day": metrics["evaluations_by_day"],
            # Контентные метрики (продуктовый апгрейд — "метрики по контенту").
            "dopx_top_matches": content["top_matches"],
            "dopx_top_players": content["top_players"],
            "dopx_rating_distribution": content["rating_distribution"],
            "dopx_matches_without_evaluations": content["matches_without_evaluations"],
        })
    except Exception:
        logger.error("dashboard_callback: не удалось посчитать KPI", exc_info=True)
    return context
