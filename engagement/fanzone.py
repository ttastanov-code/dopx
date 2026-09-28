# engagement/fanzone.py
"""Фан-зона клуба: рейтинг болельщиков за месяц, главный фанат, сравнение с соперником.
Болельщик — подписчик клуба или тот, кто в оценках указывал, что болеет за него.
Очки месяца: завершённая оценка матча клуба — 10, угаданный исход матча клуба — 5."""
from __future__ import annotations

from django.core.cache import cache
from django.db.models import Count, Q
from django.utils import timezone

EVAL_POINTS = 10
PREDICTION_POINTS = 5
TOP_SIZE = 5
CACHE_TTL = 300


def _month_start():
    now = timezone.localtime()
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def fan_ids(team) -> set:
    from evaluations.completed import completed_only
    from evaluations.models import ContextEvaluation
    from users.models import Follow

    followers = set(Follow.objects.filter(team=team, user__is_superuser=False).values_list("user_id", flat=True))
    supporters = set(
        completed_only(ContextEvaluation.objects.filter(supported_team=team, user__is_superuser=False))
        .values_list("user_id", flat=True).distinct()
    )
    return followers | supporters


def monthly_scores(team) -> dict:
    """{user_id: очки} болельщиков клуба за текущий месяц."""
    from evaluations.models import EvaluationSession
    from predictions.models import MatchPrediction
    from predictions.services import correct_prediction_q

    since = _month_start()
    fans = fan_ids(team)
    if not fans:
        return {}
    team_match = Q(match__home_team=team) | Q(match__away_team=team)
    evals = (
        EvaluationSession.objects.filter(team_match, user_id__in=fans, status="completed", completed_at__gte=since)
        .values("user_id").annotate(n=Count("id"))
    )
    correct_q = correct_prediction_q()
    predictions = (
        MatchPrediction.objects.filter(team_match, user_id__in=fans, match__status="finished", match__end_time__gte=since)
        .filter(correct_q).values("user_id").annotate(n=Count("id"))
    )
    scores = {uid: 0 for uid in fans}
    for row in evals:
        scores[row["user_id"]] += row["n"] * EVAL_POINTS
    for row in predictions:
        scores[row["user_id"]] += row["n"] * PREDICTION_POINTS
    return scores


def _summary(team) -> dict:
    # Ключи — строки: кэш должен сериализоваться в JSON.
    scores = {str(uid): pts for uid, pts in monthly_scores(team).items()}
    active = {uid: pts for uid, pts in scores.items() if pts > 0}
    return {"fans": len(scores), "active": len(active), "points": sum(active.values()), "scores": scores}


def fan_zone(team, user=None) -> dict:
    """Всё для блока на странице клуба."""
    from core.live import versioned
    from users.models import User

    key = versioned(f"fanzone:{team.pk}:{_month_start().date()}")
    data = cache.get(key)
    if data is None:
        data = _summary(team)
        rival = team.rivals.first()
        data["rival"] = {"team": rival, **{k: v for k, v in _summary(rival).items() if k != "scores"}} if rival else None
        cache.set(key, data, CACHE_TTL)

    ranked = sorted(((pts, uid) for uid, pts in data["scores"].items() if pts > 0), reverse=True)
    users = {str(u.pk): u for u in User.objects.filter(pk__in=[uid for _, uid in ranked[:TOP_SIZE]])}
    top = [{"user": users[uid], "points": pts, "place": i} for i, (pts, uid) in enumerate(ranked[:TOP_SIZE], start=1) if uid in users]

    me = None
    if user is not None and getattr(user, "is_authenticated", False) and str(user.pk) in data["scores"]:
        my_points = data["scores"][str(user.pk)]
        better = sum(1 for pts, _ in ranked if pts > my_points)
        total = len(data["scores"])
        me = {
            "points": my_points, "place": better + 1 if my_points else None,
            # Топ-X%: доля болельщиков с очками не хуже — чем меньше, тем лучше.
            "top_percent": max(1, round((better + 1) * 100 / total)) if my_points and total else None,
            "is_fan": True,
        }
    return {
        "month": _month_start(), "fans": data["fans"], "active": data["active"], "points": data["points"],
        "top": top, "leader": top[0] if top else None, "me": me, "rival": data["rival"],
    }
