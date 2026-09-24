# dashboard/services.py
"""Бизнес-логика staff-дашборда: метрики, здоровье данных, антифрод, разделы управления.
Без request/response — вьюхи тонкие.
"""
from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db.models import Count, Q
from django.db.models.functions import TruncDate
from django.utils import timezone

from analytics.selectors import daily_active_users, traffic_overview, weekly_active_users
from core.models import get_setting
from evaluations.models import (
    CoachEvaluation, ContextEvaluation, EvaluationSession, MatchEvaluation,
    PlayerEvaluation, RefereeEvaluation, TeamEvaluation,
)
from matches.models import Match
from notifications.models import ContactSubmission
from parsers.models import ParserDiscrepancy, ParserSyncRun
from users.models import SuspiciousActivityFlag
from .models import AuditAction, StaffActionLog

User = get_user_model()


# ============================================================
# Обзор метрик продукта
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
# Контентные метрики: самые оцениваемые матчи/игроки, распределение оценок,
# матчи без оценок
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

    # Распределение по contribution (1-10).
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
# Здоровье данных / синк матчей (ParserSyncRun)
# ============================================================

def data_health_summary(recent_runs: int = 20) -> dict:
    runs = list(ParserSyncRun.objects.all()[:recent_runs])
    last_run = runs[0] if runs else None

    # Матчи без составов: has_lineup=True, но строк MatchLineup нет.
    # Отдаём и count, и топ-N для ссылок и ресинка.
    matches_missing_lineups_base = Match.objects.filter(
        status__in=["live", "finished"], has_lineup=True, lineups__isnull=True
    )
    matches_missing_events_base = Match.objects.filter(
        status__in=["live", "finished"], events__isnull=True
    )
    # Точный счётчик через .count(), а не len() среза.
    matches_missing_lineups_count = matches_missing_lineups_base.count()
    matches_missing_events_count = matches_missing_events_base.count()
    # Лимит — из настроек платформы.
    matches_missing_lineups_list = list(
        matches_missing_lineups_base.select_related("home_team", "away_team").order_by("-start_time")[
            :get_setting("dashboard_missing_lineups_limit", 20)
        ]
    )
    matches_missing_events_list = list(
        matches_missing_events_base.select_related("home_team", "away_team").order_by("-start_time")[
            :get_setting("dashboard_missing_events_limit", 20)
        ]
    )

    # Расхождения импорта (ParserDiscrepancy).
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
# Центр доверия к данным
# 1. Жалобы пользователей на данные матча (ContactSubmission data_error).
# 2. ParserDiscrepancy — история, новые записи не появляются.
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
# Очередь антифрода
# ============================================================

# ============================================================
# Трафик — обёртка над analytics.selectors.traffic_overview
# ============================================================

def traffic_summary(days: int = 14) -> dict:
    return traffic_overview(days=days)


def antifraud_queue(limit: int = 25) -> dict:
    # select_related(content_type) — меньше запросов на flag.content_object.
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


# ============================================================
# Раздел «Матчи»: ручная правка + пересчёт
# ============================================================

def matches_queryset(search: str = "", status: str = "", season_id: str = ""):
    """Матчи для списка: поиск по командам, фильтры по статусу/сезону."""
    qs = Match.objects.select_related("league", "season", "home_team", "away_team")
    if search:
        qs = qs.filter(Q(home_team__name__icontains=search) | Q(away_team__name__icontains=search))
    if status:
        qs = qs.filter(status=status)
    if season_id:
        qs = qs.filter(season_id=season_id)
    return qs


# ============================================================
# Раздел «Пользователи»
# ============================================================

def users_queryset(search: str = ""):
    """Поиск пользователей по username или email."""
    User = get_user_model()
    qs = User.objects.all()
    if search:
        qs = qs.filter(Q(username__icontains=search) | Q(email__icontains=search))
    return qs.order_by("-date_joined")


def user_detail_context(user) -> dict:
    """Все данные для карточки пользователя одним вызовом."""
    return {
        "badges": list(user.badges.all().order_by("-awarded_at")[:20]),
        "push_subscriptions": list(user.push_subscriptions.all()),
        "open_flags": list(
            SuspiciousActivityFlag.objects.filter(user=user, status="pending").order_by("-created_at")[:10]
        ),
        "recent_flags_count": SuspiciousActivityFlag.objects.filter(user=user).count(),
    }


# ============================================================
# Раздел «Модерация оценок»
# EvaluationSession уникальна по (user, match). Под-оценки связаны с ней
# только через (user, match), FK нет.
# ============================================================

def evaluation_sessions_queryset(search: str = "", status: str = "", mode: str = ""):
    """Сессии для списка: поиск по username или командам."""
    qs = EvaluationSession.objects.select_related(
        "user", "match", "match__home_team", "match__away_team",
    )
    if search:
        qs = qs.filter(
            Q(user__username__icontains=search)
            | Q(match__home_team__name__icontains=search)
            | Q(match__away_team__name__icontains=search)
        )
    if status:
        qs = qs.filter(status=status)
    if mode:
        qs = qs.filter(mode=mode)
    return qs


def evaluation_session_detail_context(session: EvaluationSession) -> dict:
    """Все под-оценки этого (user, match)."""
    user, match = session.user, session.match
    return {
        "context_eval": ContextEvaluation.objects.filter(user=user, match=match).select_related("supported_team").first(),
        "team_evals": list(TeamEvaluation.objects.filter(user=user, match=match).select_related("team")),
        "player_evals": list(PlayerEvaluation.objects.filter(user=user, match=match).select_related("player").order_by("-contribution")),
        "coach_evals": list(CoachEvaluation.objects.filter(user=user, match=match).select_related("coach")),
        "referee_eval": RefereeEvaluation.objects.filter(user=user, match=match).first(),
        "match_eval": MatchEvaluation.objects.filter(user=user, match=match).first(),
    }


def evaluation_session_delete_cascade(session: EvaluationSession) -> dict:
    """Удаляет сессию и все её под-оценки. Возвращает счётчики удалённого."""
    user, match = session.user, session.match
    counts = {
        "context": ContextEvaluation.objects.filter(user=user, match=match).count(),
        "teams": TeamEvaluation.objects.filter(user=user, match=match).count(),
        "players": PlayerEvaluation.objects.filter(user=user, match=match).count(),
        "coaches": CoachEvaluation.objects.filter(user=user, match=match).count(),
        "referee": RefereeEvaluation.objects.filter(user=user, match=match).count(),
        "match_eval": MatchEvaluation.objects.filter(user=user, match=match).count(),
    }
    ContextEvaluation.objects.filter(user=user, match=match).delete()
    TeamEvaluation.objects.filter(user=user, match=match).delete()
    PlayerEvaluation.objects.filter(user=user, match=match).delete()
    CoachEvaluation.objects.filter(user=user, match=match).delete()
    RefereeEvaluation.objects.filter(user=user, match=match).delete()
    MatchEvaluation.objects.filter(user=user, match=match).delete()
    match_id = str(match.id)
    session.delete()
    counts["match_id"] = match_id
    return counts


# ============================================================
# Раздел «Партнёры и баннеры»
# ============================================================

def partners_queryset(search: str = ""):
    from partners.models import Partner
    qs = Partner.objects.all()
    if search:
        qs = qs.filter(Q(name__icontains=search) | Q(slug__icontains=search))
    return qs


def banners_queryset(zone: str = "", partner_id: str = ""):
    from partners.models import Banner
    qs = Banner.objects.select_related("partner")
    if zone:
        qs = qs.filter(zone=zone)
    if partner_id:
        qs = qs.filter(partner_id=partner_id)
    return qs
