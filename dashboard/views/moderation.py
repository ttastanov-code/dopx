# dashboard/views/moderation.py
"""Пользователи, здоровье данных, жалобы, антифрод."""
from __future__ import annotations

import csv

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth import get_user_model
from django.core.paginator import Paginator
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.admin_actions import _csv_safe
from core.models import get_setting
from matches.models import Match
from users.models import SuspiciousActivityFlag

from .. import infra_services, parser_tools, services
from ..audit import log_staff_action
from ..models import AuditAction


# ============================================================
# Пользователи. Действия обратимые (is_active=False, без удаления).
# ============================================================

USERS_PAGE_SIZE = 25


@staff_member_required
def users_list(request):
    search = request.GET.get("q", "").strip()
    qs = services.users_queryset(search=search)
    # Размер страницы — из настроек платформы (dashboard_users_page_size), по умолчанию 25.
    page_size = get_setting("dashboard_users_page_size", USERS_PAGE_SIZE)
    users_page = Paginator(qs, page_size).get_page(request.GET.get("page"))
    context = {
        "page_title": "Пользователи — DOPX Staff",
        "active_tab": "users",
        "users": users_page,
        "search": search,
    }
    return render(request, "dashboard/users_list.html", context)


@staff_member_required
def user_detail(request, user_id):
    User = get_user_model()
    user_obj = get_object_or_404(User.objects.select_related("xp"), id=user_id)
    context = {
        "page_title": f"{user_obj.username} — DOPX Staff",
        "active_tab": "users",
        "user_obj": user_obj,
        **services.user_detail_context(user_obj),
    }
    return render(request, "dashboard/user_detail.html", context)


@staff_member_required
@require_POST
def user_toggle_ban(request, user_id):
    User = get_user_model()
    user_obj = get_object_or_404(User, id=user_id)
    # Нельзя забанить самого себя.
    if user_obj.id == request.user.id:
        messages.error(request, "Нельзя заблокировать самого себя")
        return redirect("dashboard:user_detail", user_id=user_obj.id)
    # Сотрудников и суперпользователей блокирует только суперпользователь.
    if (user_obj.is_staff or user_obj.is_superuser) and not request.user.is_superuser:
        messages.error(request, "Блокировать сотрудников может только суперпользователь")
        return redirect("dashboard:user_detail", user_id=user_obj.id)

    user_obj.is_active = not user_obj.is_active
    user_obj.save(update_fields=["is_active"])

    # Голоса заблокированных в рейтинг не идут — пересчитываем его матчи.
    from aggregates.tasks import recalculate_matches_for_user
    user_id_str = str(user_obj.id)
    transaction.on_commit(lambda: recalculate_matches_for_user.delay(user_id_str))

    if user_obj.is_active:
        messages.success(request, f"{user_obj.username} разблокирован")
        log_staff_action(request, AuditAction.USER_UNBANNED, target=user_obj.username, details={"user_id": str(user_obj.id)})
    else:
        messages.success(request, f"{user_obj.username} заблокирован — вход в аккаунт закрыт")
        log_staff_action(request, AuditAction.USER_BANNED, target=user_obj.username, details={"user_id": str(user_obj.id)})
    return redirect("dashboard:user_detail", user_id=user_obj.id)


@staff_member_required
@require_POST
def user_reset_trust_score(request, user_id):
    User = get_user_model()
    user_obj = get_object_or_404(User, id=user_id)
    before = user_obj.trust_score
    user_obj.trust_score = 1.0
    user_obj.save(update_fields=["trust_score"])
    messages.success(request, f"Оценка доверия {user_obj.username} сброшена на 1.0 (была {before:.2f})")
    log_staff_action(
        request, AuditAction.USER_TRUST_SCORE_RESET,
        target=user_obj.username, details={"user_id": str(user_obj.id), "before": before, "after": 1.0},
    )
    return redirect("dashboard:user_detail", user_id=user_obj.id)


@staff_member_required
def data_health(request):
    context = {
        "page_title": "Здоровье данных — DOPX Staff",
        "active_tab": "data_health",
        "health": services.data_health_summary(),
        "infra": infra_services.infra_health(),
    }
    return render(request, "dashboard/data_health.html", context)


@staff_member_required
def data_health_partial(request):
    """Содержимое data_health без обёртки — для HTMX-поллинга."""
    context = {
        "health": services.data_health_summary(),
        "infra": infra_services.infra_health(),
    }
    return render(request, "dashboard/_data_health_content.html", context)


def _resolve_match_for_resync(match_id: str) -> Match | None:
    """Ищет матч по UUID, затем по sportmonks_id (старые записи ошибок синка)."""
    import uuid as uuid_module

    match = None
    try:
        match = Match.objects.filter(id=uuid_module.UUID(str(match_id))).first()
    except (ValueError, TypeError, AttributeError):
        pass
    if match is None:
        match = Match.objects.filter(sportmonks_id=str(match_id)).first()
    return match


@staff_member_required
@require_POST
def data_health_integrity_run(request):
    """Прогнать проверки целостности сейчас (только чтение, ~секунды)."""
    from core.integrity import run_and_store

    report = run_and_store()
    messages.success(request, f"Проверка целостности: ошибок {report['errors']}, предупреждений {report['warnings']}.")
    return redirect("dashboard:data_health")


@staff_member_required
@require_POST
def data_health_resync_match(request, match_id):
    """Синхронный полный ресинк одного матча из data-health.
    match_id — UUID или sportmonks_id (старые записи в error_samples).
    """
    match = _resolve_match_for_resync(match_id)
    if match is None:
        messages.error(request, f"Матч с id={match_id} не найден (возможно, устаревшая запись об ошибке).")
        return redirect("dashboard:data_health")

    success, message = parser_tools.resync_match(match)
    (messages.success if success else messages.error)(request, message)
    log_staff_action(
        request, AuditAction.MATCH_RESYNC,
        target=str(match), details={"match_id": str(match.id), "success": success, "message": message},
    )
    return redirect("dashboard:data_health")


@staff_member_required
def reports(request):
    """Очередь жалоб болельщиков; ?status=done — разобранные."""
    from users import reports as user_reports
    from users.models import UserReport

    done = request.GET.get("status") == "done"
    qs = (UserReport.objects.filter(status__in=["resolved", "rejected"] if done else ["new"])
          .select_related("reporter", "target_user", "friend_league__owner", "handled_by")
          .order_by("-handled_at" if done else "created_at"))
    page = Paginator(qs, 30).get_page(request.GET.get("page"))
    can_act = request.user.is_superuser or request.user.has_perm("users.change_userreport")
    labels = {k: v[0] for k, v in user_reports.ACTIONS.items()}
    rows = [{"report": r, "action_label": labels.get(r.action, r.action),
             "actions": user_reports.allowed_actions(r, request.user) if can_act and not done else []} for r in page]
    return render(request, "dashboard/reports.html", {
        "page_title": "Жалобы — DOPX Staff",
        "active_tab": "reports",
        "rows": rows,
        "page": page,
        "done": done,
        "new_count": UserReport.objects.filter(status="new").count(),
    })


@staff_member_required
@require_POST
def report_action(request, report_id):
    from users import reports as user_reports
    from users.models import UserReport

    report = get_object_or_404(UserReport.objects.select_related("target_user", "friend_league__owner"), pk=report_id)
    action = request.POST.get("action", "")
    target = report.target_label
    try:
        message = user_reports.apply(report, action, request.user)
    except user_reports.ReportError as e:
        messages.error(request, str(e))
        return redirect("dashboard:reports")
    log_staff_action(request, AuditAction.USER_REPORT_HANDLED, target=target,
                     details={"report_id": str(report.pk), "action": action, "reason": report.reason})
    messages.success(request, message)
    return redirect("dashboard:reports")


@staff_member_required
def antifraud(request):
    context = {
        "page_title": "Антифрод — DOPX Staff",
        "active_tab": "antifraud",
        "queue": services.antifraud_queue(),
    }
    return render(request, "dashboard/antifraud.html", context)


@staff_member_required
@require_POST
def antifraud_flag_action(request, flag_id):
    """Подтвердить/отклонить флаг — та же логика, что в админке."""
    flag = get_object_or_404(SuspiciousActivityFlag, id=flag_id)
    action = request.POST.get("action")

    if action not in ("confirm", "dismiss"):
        messages.error(request, "Неизвестное действие")
        return redirect("dashboard:antifraud")

    flag.status = "confirmed" if action == "confirm" else "dismissed"
    flag.reviewed_by = request.user
    flag.reviewed_at = timezone.now()
    flag.save(update_fields=["status", "reviewed_by", "reviewed_at", "updated_at"])

    if action == "dismiss":
        # «Отклонить» снимает авто-поправку рейтинга.
        from aggregates.tasks import apply_divergence_dismissal

        apply_divergence_dismissal([flag])

    # Подтверждённая накрутка исключает голоса пользователя — пересчёт затронутых матчей.
    from aggregates.tasks import schedule_recalculation_for_flags

    schedule_recalculation_for_flags([flag])

    # У entity-сигналов (vote_spike и т.п.) user пустой — цель это content_object.
    flag_target = flag.user.username if flag.user else str(flag.content_object or flag.get_source_display())

    messages.success(
        request,
        f"Флаг {'подтверждён' if action == 'confirm' else 'отклонён'}: {flag_target}",
    )
    log_staff_action(
        request,
        AuditAction.ANTIFRAUD_FLAG_CONFIRMED if action == "confirm" else AuditAction.ANTIFRAUD_FLAG_DISMISSED,
        target=flag_target,
        details={"flag_id": str(flag.id), "score": flag.score, "source": flag.source},
    )
    return redirect("dashboard:antifraud")


@staff_member_required
def antifraud_export_csv(request):
    """Выгрузка очереди антифрода (флаги + диспуты) в CSV."""
    queue = services.antifraud_queue(limit=1000)

    response = HttpResponse(content_type="text/csv")
    filename = f"antifraud_queue_{timezone.now():%Y%m%d_%H%M}.csv"
    response["Content-Disposition"] = f'attachment; filename="{filename}"'

    writer = csv.writer(response)
    writer.writerow(["Тип", "ID", "Пользователь", "Источник/тема", "Создано"])
    for flag in queue["pending_flags"]:
        # У entity-сигналов user пустой.
        who = flag.user.username if flag.user else f"[сущность] {flag.content_object or '—'}"
        # _csv_safe — защита от formula injection.
        writer.writerow([_csv_safe(v) for v in (
            "Флаг", flag.id, who, flag.get_source_display(), flag.created_at.isoformat(),
        )])
    for dispute in queue["pending_disputes"]:
        writer.writerow([_csv_safe(v) for v in (
            "Диспут",
            dispute.id,
            dispute.user.username if dispute.user else dispute.contact_email,
            dispute.subject,
            dispute.created_at.isoformat(),
        )])

    return response
