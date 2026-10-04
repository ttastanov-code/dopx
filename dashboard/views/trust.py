# dashboard/views/trust.py
"""Центр доверия к данным."""
from __future__ import annotations

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from notifications.models import ContactSubmission
from parsers.models import ParserDiscrepancy

from .. import services
from ..audit import log_staff_action
from ..models import AuditAction


# ============================================================
# Центр доверия к данным
# ============================================================

@staff_member_required
def data_trust(request):
    report_status = request.GET.get("report_status", "open")
    if report_status not in services.DATA_TRUST_REPORT_STATUS_FILTERS:
        report_status = "open"
    context = {
        "page_title": "Центр доверия к данным — DOPX Staff",
        "active_tab": "data_trust",
        "trust": services.data_trust_summary(report_status=report_status),
    }
    return render(request, "dashboard/data_trust.html", context)


@staff_member_required
@require_POST
def data_trust_resolve_report(request, submission_id):
    """Закрыть жалобу на данные матча. status: 'resolved' (по умолчанию) или 'closed'."""
    submission = get_object_or_404(ContactSubmission, id=submission_id, category="data_error")
    new_status = request.POST.get("status", "resolved")
    if new_status not in ("resolved", "closed"):
        new_status = "resolved"
    submission.status = new_status
    submission.save(update_fields=["status", "updated_at"])
    from notifications.tasks import notify_contact_resolved
    notify_contact_resolved(submission)
    messages.success(request, f"Жалоба «{submission.subject}» закрыта")
    log_staff_action(
        request, AuditAction.DATA_ERROR_REPORT_RESOLVED,
        target=submission.subject,
        details={
            "submission_id": str(submission.id),
            "match_id": str(submission.related_match_id) if submission.related_match_id else None,
            "status": new_status,
        },
    )
    return redirect(f"{reverse('dashboard:data_trust')}?report_status={request.POST.get('return_status', 'open')}")


@staff_member_required
@require_POST
def data_trust_review_discrepancy(request, discrepancy_id):
    """Отметить расхождение импорта как разобранное."""
    discrepancy = get_object_or_404(ParserDiscrepancy, id=discrepancy_id)
    discrepancy.reviewed = True
    discrepancy.reviewed_by = request.user
    discrepancy.reviewed_at = timezone.now()
    discrepancy.save(update_fields=["reviewed", "reviewed_by", "reviewed_at"])
    messages.success(request, f"Расхождение по «{discrepancy.match_label}» отмечено разобранным")
    log_staff_action(
        request, AuditAction.PARSER_DISCREPANCY_REVIEWED,
        target=discrepancy.match_label,
        details={"discrepancy_id": str(discrepancy.id), "field_name": discrepancy.field_name},
    )
    return redirect("dashboard:data_trust")
