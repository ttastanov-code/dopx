# dashboard/views/matches.py
"""Матчи: ручная правка и пересчёт агрегатов."""
from __future__ import annotations

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.utils.dateparse import parse_datetime

from matches.models import Match
from seasons.models import Season

from .. import services
from ..audit import log_staff_action
from ..models import AuditAction


# ============================================================
# Матчи: ручная правка и пересчёт агрегатов
# ============================================================

MATCHES_PAGE_SIZE = 25


@staff_member_required
def matches_list(request):
    search = request.GET.get("q", "").strip()
    status = request.GET.get("status", "")
    season_id = request.GET.get("season", "")
    qs = services.matches_queryset(search=search, status=status, season_id=season_id)
    matches_page = Paginator(qs, MATCHES_PAGE_SIZE).get_page(request.GET.get("page"))
    context = {
        "page_title": "Матчи — DOPX Staff",
        "active_tab": "matches",
        "matches": matches_page,
        "search": search,
        "status": status,
        "season_id": season_id,
        "status_choices": Match.STATUS_CHOICES,
        "seasons": Season.objects.select_related("league").order_by("-year"),
    }
    return render(request, "dashboard/matches_list.html", context)


@staff_member_required
def match_detail(request, match_id):
    match = get_object_or_404(
        Match.objects.select_related(
            "league", "season", "home_team", "away_team", "home_coach", "away_coach", "referee"
        ),
        id=match_id,
    )

    if request.method == "POST":
        before = {
            "status": match.status, "home_score": match.home_score, "away_score": match.away_score,
            "start_time": match.start_time.isoformat() if match.start_time else None,
            "tour": match.tour, "manual_override": match.manual_override,
        }
        errors: list[str] = []

        new_status = request.POST.get("status", match.status)
        if new_status not in dict(Match.STATUS_CHOICES):
            errors.append("Недопустимый статус")
        else:
            match.status = new_status

        for field in ("home_score", "away_score", "tour"):
            raw = request.POST.get(field, "").strip()
            if raw == "":
                setattr(match, field, None)
            else:
                try:
                    setattr(match, field, int(raw))
                except ValueError:
                    errors.append(f"«{field}» — должно быть целым числом")

        start_time_raw = request.POST.get("start_time", "").strip()
        if start_time_raw:
            parsed = parse_datetime(start_time_raw)
            if parsed is None:
                errors.append("Некорректный формат времени начала (ожидается ГГГГ-ММ-ДДTЧЧ:ММ)")
            else:
                match.start_time = timezone.make_aware(parsed) if timezone.is_naive(parsed) else parsed

        # Снятый чекбокс не приходит в POST.
        match.manual_override = request.POST.get("manual_override") in ("on", "1", "true")

        if errors:
            for err in errors:
                messages.error(request, err)
        else:
            match.save(update_fields=[
                "status", "home_score", "away_score", "tour", "start_time", "manual_override", "updated_at",
            ])
            messages.success(request, "Матч обновлён. Не забудьте «Пересчитать», если поменялся счёт/статус.")
            log_staff_action(
                request, AuditAction.MATCH_MANUAL_EDIT,
                target=str(match),
                details={"match_id": str(match.id), "before": before, "after": {
                    "status": match.status, "home_score": match.home_score, "away_score": match.away_score,
                    "start_time": match.start_time.isoformat() if match.start_time else None,
                    "tour": match.tour, "manual_override": match.manual_override,
                }},
            )
        return redirect("dashboard:match_detail", match_id=match.id)

    context = {
        "page_title": f"{match} — DOPX Staff",
        "active_tab": "matches",
        "match": match,
        "status_choices": Match.STATUS_CHOICES,
    }
    return render(request, "dashboard/match_detail.html", context)


@staff_member_required
@require_POST
def match_trigger_recalc(request, match_id):
    match = get_object_or_404(Match, id=match_id)
    from aggregates.tasks import recalculate_all_aggregates_for_match
    recalculate_all_aggregates_for_match.delay(str(match.id))
    messages.success(request, "Пересчёт агрегатов запущен в фоне — обновится в течение минуты.")
    log_staff_action(
        request, AuditAction.MATCH_RECALC_TRIGGERED,
        target=str(match), details={"match_id": str(match.id)},
    )
    return redirect("dashboard:match_detail", match_id=match.id)
