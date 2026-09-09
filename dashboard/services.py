# dashboard/services.py
"""
Агрегирующий слой для staff-дашборда (/staff/dashboard/...). Чистые функции
без обращения к request/response — та же дисциплина, что в
`aggregates/services.py` и `analytics/selectors.py`: вьюхи (`views.py`)
остаются тонкими диспетчерами HTTP, вся бизнес-логика подсчёта здесь, легко
тестируется без моков Django-вьюх.

Три раздела, три функции верхнего уровня:
  - overview_metrics()      — П.1 продуктовые метрики (DAU/WAU, рост, оценки)
  - data_health_summary()   — П.2 здоровье синка матчей (ParserSyncRun,
    источник-агностично — см. её собственный докстринг ниже про cutover)
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
from parsers.models import ParserDiscrepancy, ParserSyncRun
from users.models import SuspiciousActivityFlag
from .models import AuditAction, StaffActionLog

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
# П.2 — Здоровье данных / синк матчей
# ============================================================
# 2026-09-09: KFF-парсер физически удалён (по решению пользователя),
# Sportmonks — единственный источник, пишущий в ParserSyncRun (поле source
# на модели по-прежнему различает исторические KFF-строки от новых, но
# писать в него "kff" больше некому, см. parsers/models.py). "Последний
# запуск" — живой индикатор синка (см. parsers/sportmonks/tasks.py::
# _record_sync_run).

def data_health_summary(recent_runs: int = 20) -> dict:
    runs = list(ParserSyncRun.objects.all()[:recent_runs])
    last_run = runs[0] if runs else None

    # "Матчи без составов" — только те, для которых Sportmonks уже
    # подтвердил, что состав ДОЛЖЕН быть (has_lineup=True), но у нас пока
    # нет ни одной строки MatchLineup: started_at здесь не проверяем
    # отдельно, has_lineup выставляется импортёром ТОЛЬКО когда реально
    # есть что тянуть (parsers/sportmonks/importers.py::import_lineups),
    # так что пересечение с отсутствием строк состава уже точное. У
    # матчей из KFF-истории (до 2026-09-09) has_lineup тоже мог быть
    # выставлен старым, уже удалённым импортёром — поле на модели не
    # трогали, значение осталось.
    #
    # Отдаём не только .count(), но и сам queryset (топ-N) — чтобы в
    # шаблоне сразу дать ссылку на матч + кнопку ресинка, без похода в admin.
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

    # Расхождения импорта (аудит 2026-09-04, см. parsers/models.py::
    # ParserDiscrepancy) — правки счёта/статуса задним числом поверх уже
    # завершённых матчей. Отдельная карточка на дашборде, не смешиваем со
    # "статистикой синка" выше: это не ошибка запроса к API, а сигнал
    # "данные пришли успешно, но разошлись с тем, что мы уже считали фактом".
    unreviewed_discrepancies = ParserDiscrepancy.objects.filter(reviewed=False)

    return {
        "last_run": last_run,
        "recent_runs": runs,
        "matches_missing_lineups": matches_missing_lineups_count,
        "matches_missing_lineups_list": matches_missing_lineups_list,
        "matches_missing_events": matches_missing_events_count,
        "matches_missing_events_list": matches_missing_events_list,
        "recent_error_samples": (last_run.error_samples if last_run else [])[:10],
        "unreviewed_discrepancies_count": unreviewed_discrepancies.count(),
        "unreviewed_discrepancies_list": list(unreviewed_discrepancies.select_related("match")[:20]),
    }


# ============================================================
# Центр доверия к данным (2026-09-09) — MVP из рекомендации Codex-ревью,
# явно подтверждённой пользователем как отдельная задача, затем расширен
# по прямой просьбе пользователя ("сделай страницу более функциональной").
# Намеренно НЕ дублирует data_health_summary() выше: та отвечает "синк
# работает технически?" (ошибки API, отсутствующие составы/события), эта —
# "можно ли доверять УЖЕ импортированным данным конкретного матча?".
#
# УДАЛЕНО (2026-09-09, решение пользователя): очередь "Стадионы, требующие
# проверки" убрана вместе со всей моделью Stadium — оказалось, что проблема
# была не в отдельных ошибках сопоставления, а принципиальная: клубы КПЛ
# реально играют "домашние" матчи на разных стадионах в разных городах в
# течение сезона, доверять venue-данным Sportmonks в принципе нельзя. См.
# matches/models.py и core/models_stadium.py (модель удалена).
#
# Две живые очереди + расширенная история:
#   1. ContactSubmission(category='data_error') — жалобы пользователей на
#      конкретный матч (see notifications/models.py, templates/matches/
#      _match_header.html — кнопка "Сообщить об ошибке в данных"). Теперь с
#      фильтром "открытые/решённые/все" (data_trust_summary(status_filter=)).
#   2. ParserDiscrepancy — расхождения импорта. ВАЖНО: писал их только
#      старый KFF-импортёр (удалён 2026-09-09) — очередь ЗАМОРОЖЕНА,
#      Sportmonks-пайплайн новых строк сюда не пишет. Показываем как
#      историю, честно помечено в шаблоне, но действие "разобрать" полезно
#      и для старых записей. Раньше здесь был только счётчик+ссылка на
#      data-health — теперь полноценная queue с действием прямо на этой
#      странице (не нужно уходить в admin ради одного клика).
# ============================================================

DATA_TRUST_HISTORY_ACTIONS = [
    AuditAction.DATA_ERROR_REPORT_RESOLVED,
    AuditAction.MATCH_RESYNC,
    AuditAction.PARSER_DISCREPANCY_REVIEWED,
]

DATA_TRUST_REPORT_STATUS_FILTERS = {
    "open": ["new", "in_progress"],
    "resolved": ["resolved", "closed"],
    "all": ["new", "in_progress", "resolved", "closed"],
}


def data_trust_summary(limit: int = 25, report_status: str = "open") -> dict:
    statuses = DATA_TRUST_REPORT_STATUS_FILTERS.get(report_status, DATA_TRUST_REPORT_STATUS_FILTERS["open"])
    data_error_reports_qs = (
        ContactSubmission.objects.filter(category="data_error", status__in=statuses)
        .select_related("user", "related_match", "related_match__home_team", "related_match__away_team")
        .order_by("-created_at")
    )
    all_data_error_reports = ContactSubmission.objects.filter(category="data_error")
    unreviewed_discrepancies = ParserDiscrepancy.objects.filter(reviewed=False).select_related("match")
    recent_corrections = list(
        StaffActionLog.objects.filter(action__in=DATA_TRUST_HISTORY_ACTIONS)
        .select_related("actor")
        .order_by("-created_at")[:limit]
    )

    return {
        "report_status_filter": report_status,
        "data_error_reports_count": data_error_reports_qs.count(),
        "data_error_reports": list(data_error_reports_qs[:limit]),
        "data_error_reports_total": all_data_error_reports.count(),
        "data_error_reports_resolved_count": all_data_error_reports.filter(status__in=["resolved", "closed"]).count(),
        "unreviewed_discrepancies_count": unreviewed_discrepancies.count(),
        "unreviewed_discrepancies": list(unreviewed_discrepancies.order_by("-created_at")[:limit]),
        "recent_corrections": recent_corrections,
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
    # select_related("content_type") — GenericForeignKey (content_object)
    # сам по себе не поддерживает select_related, но подгрузка ContentType
    # заранее убирает один из двух хопов, которые Django делает при первом
    # обращении к flag.content_object в шаблоне (см. anti-brigading,
    # source="vote_spike" — entity-level флаги без user).
    pending_flags = list(
        SuspiciousActivityFlag.objects.filter(status="pending")
        .select_related("user", "match", "content_type")
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
