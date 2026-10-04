# dashboard/views/names.py
"""Проверка ФИО и дубли игроков."""
from __future__ import annotations

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.models import NAME_SOURCE_AI_VERIFIED
from parsers import name_ai
from parsers.models import ConfirmedNameCorrection, NameVerificationSuggestion
from players.models import Player, PotentialDuplicatePlayer

from ..audit import log_staff_action
from ..models import AuditAction


# ============================================================
# Проверка ФИО (ИИ): ручное подтверждение предложений Gemini или Claude.
# ============================================================

def _names_review_queue_context() -> dict:
    """Живая часть очереди (ждут проверки / ошибки ИИ) для страницы и HTMX-поллинга.
    «Недавно разобранные» с пагинацией сюда не входят.
    """
    pending = NameVerificationSuggestion.objects.filter(status="pending_review").order_by("-created_at")
    # Для массового подтверждения — только где ИИ согласен с текущим написанием.
    matches_count = pending.filter(matches_current=True).count()
    # Честный общий счётчик ошибок до среза [:20].
    failed_qs = NameVerificationSuggestion.objects.filter(status="check_failed").order_by("-created_at")
    failed_count = failed_qs.count()
    failed = failed_qs[:20]
    return {
        "pending_suggestions": pending,
        "failed_suggestions": failed,
        "failed_count": failed_count,
        "matches_count": matches_count,
        "names_ai": name_ai.provider_label(),
    }


@staff_member_required
def names_review(request):
    # «Недавно разобранные» — постранично по 20.
    recent_decided_qs = (
        NameVerificationSuggestion.objects.filter(status__in=["approved", "rejected"])
        .select_related("reviewed_by").order_by("-reviewed_at")
    )
    recent_decided_paginator = Paginator(recent_decided_qs, 20)
    recent_decided = recent_decided_paginator.get_page(request.GET.get("page"))
    context = {
        "page_title": "Проверка ФИО (ИИ) — DOPX Staff",
        "active_tab": "names_review",
        **_names_review_queue_context(),
        "recent_decided": recent_decided,
        "gemini_configured": name_ai.is_configured(),
    }
    return render(request, "dashboard/names_review.html", context)


@staff_member_required
def names_review_partial(request):
    """Карточки очереди для HTMX-поллинга (раз в 6 с — внутри есть поля ввода)."""
    return render(request, "dashboard/_names_review_queue.html", _names_review_queue_context())


@staff_member_required
@require_POST
def names_review_bulk_confirm_matches(request):
    """Массово подтверждает только те предложения, где Gemini сказал, что текущее
    написание верное. Реальные исправления разбираются по одному.
    name_source всегда становится ai_verified.
    """
    qs = NameVerificationSuggestion.objects.filter(status="pending_review", matches_current=True)
    confirmed = 0
    skipped = 0
    for suggestion in qs:
        entity = suggestion.content_object
        if entity is None:
            suggestion.status = "rejected"
            suggestion.reviewed_by = request.user
            suggestion.reviewed_at = timezone.now()
            suggestion.save(update_fields=["status", "reviewed_by", "reviewed_at", "updated_at"])
            skipped += 1
            continue

        final_first = (suggestion.suggested_first_name or entity.first_name).strip()
        final_last = (suggestion.suggested_last_name or entity.last_name).strip()
        update_fields = []
        if final_first and final_first != entity.first_name:
            ConfirmedNameCorrection.objects.update_or_create(
                wrong_text=entity.first_name.strip().lower(),
                defaults={"correct_text": final_first, "source_suggestion": suggestion, "created_by": request.user},
            )
            entity.first_name = final_first
            update_fields.append("first_name")
        if final_last and final_last != entity.last_name:
            ConfirmedNameCorrection.objects.update_or_create(
                wrong_text=entity.last_name.strip().lower(),
                defaults={"correct_text": final_last, "source_suggestion": suggestion, "created_by": request.user},
            )
            entity.last_name = final_last
            update_fields.append("last_name")

        entity.name_source = NAME_SOURCE_AI_VERIFIED
        update_fields.append("name_source")
        entity.save(update_fields=update_fields + ["updated_at"])

        suggestion.status = "approved"
        suggestion.reviewed_by = request.user
        suggestion.reviewed_at = timezone.now()
        suggestion.save(update_fields=["status", "reviewed_by", "reviewed_at", "updated_at"])
        confirmed += 1

    log_staff_action(
        request, AuditAction.NAME_SUGGESTION_APPROVED,
        target="bulk_confirm_matches",
        details={"confirmed": confirmed, "skipped_deleted_entity": skipped},
    )
    suffix = f", пропущено (сущность удалена): {skipped}" if skipped else ""
    messages.success(request, f"Массово подтверждено: {confirmed}{suffix}")
    return redirect("dashboard:names_review")


@staff_member_required
@require_POST
def names_review_action(request, suggestion_id):
    """approve — пишет ConfirmedNameCorrection и сразу обновляет сущность.
    reject/скрыть ошибку — только очередь, данные сайта не трогает.
    Имя берём из формы: staff мог поправить предложение Gemini. Логика — review_actions.
    """
    from .. import review_actions

    suggestion = get_object_or_404(NameVerificationSuggestion, id=suggestion_id)
    action = request.POST.get("action")
    if action not in ("approve", "reject"):
        messages.error(request, f"Неизвестное действие: {action}")
        return redirect("dashboard:names_review")
    if action == "reject":
        result = review_actions.reject_name(suggestion, request.user)
    else:
        result = review_actions.approve_name(suggestion, request.user, request.POST.get("first_name"), request.POST.get("last_name"))
    if result.action:
        log_staff_action(request, result.action, target=result.target, details=result.details)
    (messages.success if result.ok else messages.warning)(request, result.message)
    return redirect("dashboard:names_review")


def _player_dup_stats(player: Player) -> dict:
    """Сводка по дублям игроков для очереди."""
    return {
        "player": player,
        "appearances": player.matchlineupplayer_set.count(),
        "events_count": player.events.count(),
        "evaluations_count": player.player_evaluations.count(),
        "aggregates_count": player.match_aggregates.count(),
    }


@staff_member_required
def duplicate_players_review(request):
    """Очередь «Дубли игроков». Флаги ставит импорт при совпадении ФИО в команде.
    Слияние выполняется синхронно во вьюхе.
    """
    flags = list(
        PotentialDuplicatePlayer.objects.filter(reviewed=False)
        .select_related("existing_player__team", "new_player__team")
        .order_by("-created_at")
    )
    pairs = [
        {
            "flag": flag,
            "existing": _player_dup_stats(flag.existing_player),
            "new": _player_dup_stats(flag.new_player),
        }
        for flag in flags
    ]
    return render(request, "dashboard/duplicate_players_review.html", {
        "page_title": "Дубли игроков — DOPX Staff",
        "active_tab": "duplicate_players",
        "pairs": pairs,
        "pending_count": len(pairs),
    })


@staff_member_required
@require_POST
def duplicate_players_merge(request, flag_id):
    """keep=existing|new — какую запись оставить, вторая сливается и удаляется.
    HTMX получает пустой партиал на место карточки, обычный POST — редирект.
    """
    from .. import review_actions

    flag = get_object_or_404(PotentialDuplicatePlayer, id=flag_id)
    result = review_actions.merge_duplicate(flag, request.POST.get("keep"), request.user)
    return _duplicate_result(request, result)


@staff_member_required
@require_POST
def duplicate_players_dismiss(request, flag_id):
    """Не дубль (разные люди) — просто отмечаем флаг разобранным."""
    from .. import review_actions

    flag = get_object_or_404(PotentialDuplicatePlayer, id=flag_id)
    return _duplicate_result(request, review_actions.dismiss_duplicate(flag, request.user))


def _duplicate_result(request, result):
    if result.action:
        log_staff_action(request, result.action, target=result.target, details=result.details)
    if result.ok and request.headers.get("HX-Request"):
        return render(request, "dashboard/_duplicate_players_resolved.html", {"message": result.message})
    (messages.success if result.ok else messages.warning)(request, result.message)
    return redirect("dashboard:duplicate_players_review")
