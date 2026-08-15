# dashboard/services.py
"""
Агрегирующий слой для staff-дашборда (/staff/dashboard/...). Чистые функции
без обращения к request/response — та же дисциплина, что в
`aggregates/services.py` и `analytics/selectors.py`: вьюхи (`views.py`)
остаются тонкими диспетчерами HTTP, вся бизнес-логика подсчёта здесь, легко
тестируется без моков Django-вьюх.

Три раздела, три функции верхнего уровня:
  - overview_metrics()      — П.1 продуктовые метрики (DAU/WAU, рост, оценки)
  - data_health_summary()   — П.2 здоровье KFF-синка (ParserSyncRun)
  - antifraud_queue()       — П.3 быстрый триаж SuspiciousActivityFlag/диспутов
"""
from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db.models import Count
from django.db.models.functions import TruncDate
from django.utils import timezone

from analytics.selectors import daily_active_users, traffic_overview, weekly_active_users
from evaluations.models import ContextEvaluation, PlayerEvaluation
from matches.models import Match
from notifications.models import ContactSubmission
from parsers.models import ParserSyncRun
from users.models import SuspiciousActivityFlag

User = get_user_model()


# ============================================================
# П.1 — Обзор метрик продукта
# ============================================================

def overview_metrics(days: int = 14) -> dict:
    since = timezone.now() - timedelta(days=days)

    total_users = User.objects.count()
    verified_users = User.objects.filter(is_verified=True).count()

    registrations_by_day = list(
        User.objects.filter(date_joined__gte=since)
        .annotate(period=TruncDate("date_joined"))
        .values("period")
        .annotate(count=Count("id"))
        .order_by("period")
    )
    evaluations_by_day = list(
        ContextEvaluation.objects.filter(created_at__gte=since)
        .annotate(period=TruncDate("created_at"))
        .values("period")
        .annotate(count=Count("id"))
        .order_by("period")
    )

    return {
        "total_users": total_users,
        "verified_users": verified_users,
        "verification_rate_percent": round(verified_users / total_users * 100, 1) if total_users else 0.0,
        "new_users_period": User.objects.filter(date_joined__gte=since).count(),
        "total_evaluations": ContextEvaluation.objects.count(),
        "evaluations_period": ContextEvaluation.objects.filter(created_at__gte=since).count(),
        "live_matches": Match.objects.filter(status="live").count(),
        "scheduled_matches": Match.objects.filter(status="scheduled").count(),
        "registrations_by_day": [
            {"period": r["period"].isoformat(), "count": r["count"]} for r in registrations_by_day
        ],
        "evaluations_by_day": [
            {"period": r["period"].isoformat(), "count": r["count"]} for r in evaluations_by_day
        ],
        "dau": daily_active_users(days=days),
        "wau": weekly_active_users(weeks=max(4, days // 7)),
    }


# ============================================================
# Контентные метрики — для главной /admin/ (продуктовый апгрейд, "метрики
# по контенту": какие матчи/игроки набирают больше всего оценок, как
# распределены выставленные оценки, у скольких сыгранных матчей вообще
# нет ни одной оценки от пользователей.
# ============================================================

def content_metrics(limit: int = 8) -> dict:
    top_matches = list(
        ContextEvaluation.objects.values("match_id", "match__home_team__name", "match__away_team__name")
        .annotate(evals=Count("id")).order_by("-evals")[:limit]
    )
    top_players = list(
        PlayerEvaluation.objects.values("player_id", "player__first_name", "player__last_name")
        .annotate(evals=Count("id")).order_by("-evals")[:limit]
    )

    # Распределение оценок игроков по "вкладу" (contribution, 1-10) — из
    # трёх полей PlayerEvaluation (contribution/risk/potential) contribution
    # ближе всего к общей "итоговой оценке" в восприятии staff.
    bucket_labels = ["1-2", "3-4", "5-6", "7-8", "9-10"]
    buckets = {label: 0 for label in bucket_labels}
    for value in PlayerEvaluation.objects.values_list("contribution", flat=True):
        if value <= 2:
            buckets["1-2"] += 1
        elif value <= 4:
            buckets["3-4"] += 1
        elif value <= 6:
            buckets["5-6"] += 1
        elif value <= 8:
            buckets["7-8"] += 1
        else:
            buckets["9-10"] += 1

    matches_without_evaluations = (
        Match.objects.filter(status="finished")
        .exclude(id__in=ContextEvaluation.objects.values("match_id"))
        .count()
    )

    return {
        "top_matches": [
            {
                "match_id": row["match_id"],
                "label": f"{row['match__home_team__name']} — {row['match__away_team__name']}",
                "evals": row["evals"],
            }
            for row in top_matches
        ],
        "top_players": [
            {
                "player_id": row["player_id"],
                "label": f"{row['player__first_name']} {row['player__last_name']}",
                "evals": row["evals"],
            }
            for row in top_players
        ],
        "rating_distribution": buckets,
        "matches_without_evaluations": matches_without_evaluations,
    }


# ============================================================
# П.2 — Здоровье данных / KFF-синк
# ============================================================

def data_health_summary(recent_runs: int = 20) -> dict:
    runs = list(ParserSyncRun.objects.all()[:recent_runs])
    last_run = runs[0] if runs else None

    # "Матчи без составов" — только те, для которых уже наступило время,
    # когда состав ДОЛЖЕН быть (has_lineup=True со стороны KFF, но у нас
    # пока нет lineups) или матч уже live/finished, а состава так и нет:
    # started_at здесь не проверяем отдельно, has_lineup — это ФЛАГ ОТ KFF
    # "состав опубликован", он появляется только когда реально есть что
    # тянуть, так что пересечение с отсутствием locale-записи уже точное.
    #
    # ИСПРАВЛЕНО: раньше отдавали только .count() — цифра на дашборде без
    # возможности узнать, КАКОЙ именно это матч, вынуждала руками перебирать
    # список из сотен матчей в Django admin. Теперь отдаём ещё и сам
    # queryset (обрезанный до разумного топ-N — этих матчей штучно, но на
    # всякий случай не грузим весь список без лимита), чтобы в шаблоне
    # сразу дать ссылку на матч + кнопку ресинка одним кликом.
    matches_missing_lineups_base = Match.objects.filter(
        status__in=["live", "finished"], has_lineup=True, lineups__isnull=True
    )
    matches_missing_events_base = Match.objects.filter(
        status__in=["live", "finished"], events__isnull=True
    )
    # Точный счётчик — отдельный .count() (индексированный запрос, дешёвый),
    # НЕ len() от обрезанного [:20]-списка ниже — иначе цифра на карточке
    # молча занижалась бы, если проблемных матчей вдруг окажется больше 20.
    matches_missing_lineups_count = matches_missing_lineups_base.count()
    matches_missing_events_count = matches_missing_events_base.count()
    matches_missing_lineups_list = list(
        matches_missing_lineups_base.select_related("home_team", "away_team").order_by("-start_time")[:20]
    )
    matches_missing_events_list = list(
        matches_missing_events_base.select_related("home_team", "away_team").order_by("-start_time")[:20]
    )

    return {
        "last_run": last_run,
        "recent_runs": runs,
        "matches_missing_lineups": matches_missing_lineups_count,
        "matches_missing_lineups_list": matches_missing_lineups_list,
        "matches_missing_events": matches_missing_events_count,
        "matches_missing_events_list": matches_missing_events_list,
        "recent_error_samples": (last_run.error_samples if last_run else [])[:10],
    }


# ============================================================
# П.3 — Очередь антифрода
# ============================================================

# ============================================================
# Трафик и посещаемость — тонкая обёртка над analytics.selectors.traffic_overview,
# сохраняем единый паттерн вызова services.X() из dashboard/views.py, как и
# у трёх функций выше.
# ============================================================

def traffic_summary(days: int = 14) -> dict:
    return traffic_overview(days=days)


def antifraud_queue(limit: int = 25) -> dict:
    pending_flags = list(
        SuspiciousActivityFlag.objects.filter(status="pending")
        .select_related("user", "match")
        .order_by("-score", "-created_at")[:limit]
    )
    pending_disputes = list(
        ContactSubmission.objects.filter(category="dispute", status__in=["new", "in_progress"])
        .select_related("user")
        .order_by("-created_at")[:limit]
    )
    return {
        "pending_flags": pending_flags,
        "pending_flags_count": SuspiciousActivityFlag.objects.filter(status="pending").count(),
        "pending_disputes": pending_disputes,
        "pending_disputes_count": ContactSubmission.objects.filter(
            category="dispute", status__in=["new", "in_progress"]
        ).count(),
    }
