# users/city_stats.py
"""Статистика по городам пользователей: битва городов, разрез для дашборда, география болельщиков клуба."""
from __future__ import annotations

from datetime import timedelta

from django.core.cache import cache
from django.db.models import Case, CharField, Count, F, Q, Value, When
from django.utils import timezone

from core.models import get_setting
from evaluations.completed import completed_only
from evaluations.models import ContextEvaluation, EvaluationSession
from predictions.models import MatchPrediction
from users.models import User

# Город показываем публично только при таком числе пользователей в нём.
CITY_MIN_USERS_DEFAULT = 10
# Карту болельщиков клуба показываем при таком числе болельщиков с городом.
TEAM_FAN_GEO_MIN_FANS_DEFAULT = 10
TEAM_FAN_GEO_TOP = 5
CACHE_TTL = 60 * 30

BATTLE_PERIODS = {
    "month": {"label": "30 дней", "days": 30},
    "season": {"label": "90 дней", "days": 90},
    "all": {"label": "Всё время", "days": None},
}


def city_min_users() -> int:
    return get_setting("city_stats_min_users", CITY_MIN_USERS_DEFAULT)


def team_fan_geo_min_fans() -> int:
    return get_setting("team_fan_geo_min_fans", TEAM_FAN_GEO_MIN_FANS_DEFAULT)


def _real_users():
    return User.objects.filter(is_active=True, is_verified=True).exclude(city="")


def _final_result_expr():
    """'1' / 'X' / '2' по счёту матча — то же, что Match.final_result, но в SQL."""
    return Case(
        When(match__home_score__gt=F("match__away_score"), then=Value("1")),
        When(match__home_score__lt=F("match__away_score"), then=Value("2")),
        default=Value("X"),
        output_field=CharField(),
    )


def city_battle(period: str = "month") -> list[dict]:
    """Рейтинг городов за период. Основная метрика — оценок на пользователя
    (чтобы большой город не выигрывал только численностью).

    Элемент: {city, users, active_users, evaluations, per_user, predictions, accuracy}.
    Города с users < city_min_users() не попадают.
    """
    if period not in BATTLE_PERIODS:
        period = "month"
    min_users = city_min_users()
    cache_key = f"city_battle:{period}:{min_users}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    days = BATTLE_PERIODS[period]["days"]
    since = timezone.now() - timedelta(days=days) if days else None

    users_by_city = dict(
        _real_users().values("city").annotate(n=Count("id")).values_list("city", "n")
    )
    cities = {c for c, n in users_by_city.items() if n >= min_users}
    if not cities:
        cache.set(cache_key, [], CACHE_TTL)
        return []

    sessions = EvaluationSession.objects.filter(
        status="completed", user__city__in=cities, user__is_active=True, user__is_verified=True,
    )
    if since:
        sessions = sessions.filter(completed_at__gte=since)
    eval_stats = {
        row["user__city"]: row
        for row in sessions.values("user__city").annotate(
            evaluations=Count("id"), active_users=Count("user", distinct=True),
        )
    }

    preds = MatchPrediction.objects.filter(
        user__city__in=cities, user__is_active=True, user__is_verified=True,
        match__status="finished", match__home_score__isnull=False, match__away_score__isnull=False,
    )
    if since:
        preds = preds.filter(match__start_time__gte=since)
    pred_stats = {
        row["user__city"]: row
        for row in preds.annotate(result=_final_result_expr()).values("user__city").annotate(
            total=Count("id"), correct=Count("id", filter=Q(choice=F("result"))),
        )
    }

    rows = []
    for city in cities:
        users = users_by_city[city]
        ev = eval_stats.get(city, {})
        pr = pred_stats.get(city, {})
        evaluations = ev.get("evaluations", 0)
        pred_total = pr.get("total", 0)
        rows.append({
            "city": city,
            "users": users,
            "active_users": ev.get("active_users", 0),
            "evaluations": evaluations,
            "per_user": round(evaluations / users, 1),
            "predictions": pred_total,
            # Точность — только при 20+ прогнозах, иначе случайность.
            "accuracy": round(100 * pr.get("correct", 0) / pred_total) if pred_total >= 20 else None,
        })
    rows.sort(key=lambda r: (-r["per_user"], -r["evaluations"], r["city"]))
    for i, r in enumerate(rows, start=1):
        r["rank"] = i
    cache.set(cache_key, rows, CACHE_TTL)
    return rows


def dashboard_city_breakdown(days: int = 14) -> dict:
    """Разрез пользователей по городам для staff (без порога минимума).

    rows: {city, users, new_users, evaluated_ever, conversion_percent, active_period}.
    Пустой город — «Не указан».
    """
    since = timezone.now() - timedelta(days=days)
    base = User.objects.filter(is_active=True, is_staff=False)

    totals = {
        row["city"]: row
        for row in base.values("city").annotate(
            users=Count("id"),
            new_users=Count("id", filter=Q(date_joined__gte=since)),
        )
    }
    evaluated = dict(
        EvaluationSession.objects.filter(status="completed", user__in=base)
        .values("user__city").annotate(n=Count("user", distinct=True))
        .values_list("user__city", "n")
    )
    active = dict(
        EvaluationSession.objects.filter(status="completed", user__in=base, completed_at__gte=since)
        .values("user__city").annotate(n=Count("user", distinct=True))
        .values_list("user__city", "n")
    )

    rows = []
    for city, t in totals.items():
        ever = evaluated.get(city, 0)
        rows.append({
            "city": city or "Не указан",
            "users": t["users"],
            "new_users": t["new_users"],
            "evaluated_ever": ever,
            "conversion_percent": round(100 * ever / t["users"]) if t["users"] else 0,
            "active_period": active.get(city, 0),
        })
    rows.sort(key=lambda r: (-r["users"], r["city"]))
    return {
        "rows": rows,
        "cities_count": sum(1 for r in rows if r["city"] != "Не указан"),
        "no_city_users": totals.get("", {}).get("users", 0),
    }


def team_fan_geography(team) -> dict | None:
    """Где живут болельщики клуба: пользователи, хоть раз указавшие команду как свою
    при оценке (ContextEvaluation.supported_team), сгруппированные по городу.

    {total, rows: [{city, fans, percent}], other_fans, other_percent} или None,
    если болельщиков с городом меньше team_fan_geo_min_fans().
    """
    min_fans = team_fan_geo_min_fans()
    cache_key = f"team_fan_geo:{team.pk}:{min_fans}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached or None

    fan_ids = completed_only(ContextEvaluation.objects.filter(supported_team=team)).values("user_id").distinct()
    by_city = list(
        _real_users().filter(id__in=fan_ids)
        .values("city").annotate(fans=Count("id")).order_by("-fans", "city")
    )
    total = sum(r["fans"] for r in by_city)
    if total < min_fans:
        cache.set(cache_key, {}, CACHE_TTL)
        return None

    top = by_city[:TEAM_FAN_GEO_TOP]
    other = total - sum(r["fans"] for r in top)
    result = {
        "total": total,
        "rows": [
            {"city": r["city"], "fans": r["fans"], "percent": round(100 * r["fans"] / total)}
            for r in top
        ],
        "other_fans": other,
        "other_percent": round(100 * other / total) if other else 0,
    }
    cache.set(cache_key, result, CACHE_TTL)
    return result
