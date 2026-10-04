# dashboard/views/system.py
"""Журнал, объявления, сессии оценки, сервисы и статус."""
from __future__ import annotations

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.core.paginator import Paginator
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from core.models import get_setting
from evaluations.models import EvaluationSession

from .. import infra_services, services
from ..audit import log_staff_action
from ..models import AuditAction, StaffActionLog


@staff_member_required
def audit_log(request):
    """Журнал кастомных staff-действий (StaffActionLog). CRUD из админки — в django_admin_log."""
    # Размер журнала — из настроек платформы.
    entries_limit = get_setting("audit_log_entries_limit", 200)
    entries = list(StaffActionLog.objects.select_related("actor")[:entries_limit])
    context = {
        "page_title": "Аудит — DOPX Staff",
        "active_tab": "audit",
        "entries": entries,
    }
    return render(request, "dashboard/audit_log.html", context)


# ============================================================
# Объявления: системная рассылка всем пользователям.
# ============================================================

@staff_member_required
def announcements(request):
    """Рассылка объявления всем верифицированным пользователям.
    In-app — сразу одним bulk_create, email — пачками через Celery (с учётом
    email_system). Рейт-лимит на staff защищает от двойной отправки.
    """
    from core.utils import is_rate_limited
    from notifications.models import Notification
    from notifications.tasks import BULK_EMAIL_CHUNK_SIZE, _chunked, _send_system_announcement_chunk
    from users.models import User

    recipients_count = User.objects.filter(is_verified=True).count()

    if request.method == "POST":
        title = request.POST.get("title", "").strip()
        body = request.POST.get("body", "").strip()

        if not title or not body:
            messages.error(request, "Заполните заголовок и текст объявления")
        elif is_rate_limited(f"system_announcement:{request.user.id}", 3, 300):
            messages.error(request, "Слишком много рассылок подряд. Подождите пару минут (это защита от случайного дубля).")
        else:
            verified_users = list(User.objects.filter(is_verified=True))

            # In-app всем сразу; email_system влияет только на письмо.
            Notification.objects.bulk_create([
                Notification(
                    user=u, notification_type='system',
                    title=title, message=body, is_read=False,
                )
                for u in verified_users
            ])

            user_ids_with_email = [str(u.id) for u in verified_users if u.email]
            subject = f'{title} | DOPX'
            chunks = _chunked(user_ids_with_email, BULK_EMAIL_CHUNK_SIZE)
            for chunk in chunks:
                _send_system_announcement_chunk.delay(chunk, subject, title, body)

            messages.success(
                request,
                f"Объявление отправлено: {len(verified_users)} in-app, "
                f"{len(user_ids_with_email)} писем в очереди ({len(chunks)} пачек)",
            )
            log_staff_action(
                request, AuditAction.SYSTEM_ANNOUNCEMENT_SENT,
                target=title,
                details={
                    "title": title,
                    "body_preview": body[:200],
                    "recipients_in_app": len(verified_users),
                    "recipients_email": len(user_ids_with_email),
                },
            )
            return redirect("dashboard:announcements")

    context = {
        "page_title": "Объявления — DOPX Staff",
        "active_tab": "announcements",
        "recipients_count": recipients_count,
    }
    return render(request, "dashboard/announcements.html", context)


EVALUATION_SESSIONS_PAGE_SIZE = 25


@staff_member_required
def evaluation_sessions_list(request):
    """Модерация оценок: поиск и фильтр сессий, свежие сверху."""
    search = request.GET.get("q", "").strip()
    status_filter = request.GET.get("status", "").strip()
    mode_filter = request.GET.get("mode", "").strip()
    qs = services.evaluation_sessions_queryset(search=search, status=status_filter, mode=mode_filter)
    sessions_page = Paginator(qs, EVALUATION_SESSIONS_PAGE_SIZE).get_page(request.GET.get("page"))
    context = {
        "page_title": "Модерация оценок — DOPX Staff",
        "active_tab": "evaluation_sessions",
        "sessions": sessions_page,
        "search": search,
        "status_filter": status_filter,
        "mode_filter": mode_filter,
        "status_choices": EvaluationSession.STATUS_CHOICES,
        "mode_choices": EvaluationSession.MODE_CHOICES,
    }
    return render(request, "dashboard/evaluation_sessions_list.html", context)


@staff_member_required
def evaluation_session_detail(request, session_id):
    session = get_object_or_404(
        EvaluationSession.objects.select_related("user", "match", "match__home_team", "match__away_team"),
        id=session_id,
    )
    context = {
        "page_title": f"Оценка: {session.user.username} — DOPX Staff",
        "active_tab": "evaluation_sessions",
        "session": session,
        **services.evaluation_session_detail_context(session),
    }
    return render(request, "dashboard/evaluation_session_detail.html", context)


@staff_member_required
@require_POST
def evaluation_session_delete(request, session_id):
    """Удаляет сессию оценки со всеми её оценками и запускает пересчёт матча."""
    session = get_object_or_404(
        EvaluationSession.objects.select_related("user", "match"), id=session_id,
    )
    username = session.user.username
    match_str = str(session.match)
    match_id = str(session.match.id)
    with transaction.atomic():
        counts = services.evaluation_session_delete_cascade(session)

    # Сразу, а не очередью: у закрытого матча фонового пересчёта больше не будет, и потерянная задача
    # оставила бы в рейтинге удалённые голоса.
    from aggregates.tasks import recalculate_match_now
    recalculate_match_now(match_id)

    total_deleted = sum(v for k, v in counts.items() if k != "match_id")
    messages.success(
        request,
        f"Сессия «{username} — {match_str}» удалена вместе с {total_deleted} под-оценками. "
        f"Рейтинги матча пересчитаны.",
    )
    log_staff_action(
        request, AuditAction.EVALUATION_SESSION_DELETED,
        target=f"{username} — {match_str}",
        details={"user": username, "match_id": match_id, **counts},
    )
    return redirect("dashboard:evaluation_sessions_list")


@staff_member_required
@require_POST
def service_restart(request, name):
    """Перезапуск процесса по кнопке: флаг в Redis, процесс сам завершается, Docker поднимает заново."""
    from core import heartbeat

    if not request.user.is_superuser:
        messages.error(request, "Перезапуск сервисов — только для суперпользователя.")
        return redirect("dashboard:system_status")
    row = next((r for r in heartbeat.overview() if r["name"] == name and r["restartable"]), None)
    if row is None:
        messages.error(request, "Этот сервис так не перезапускается.")
        return redirect("dashboard:system_status")
    heartbeat.request_restart(name)
    log_staff_action(request, AuditAction.SERVICE_RESTART, target=f"Перезапуск: {row['label']}", details={"service": name})
    messages.success(request, f"«{row['label']}» перезапустится в течение минуты. На проде Docker поднимет его сам, а "
                              "на ноутбуке процесс просто остановится, запустите его заново.")
    return redirect("dashboard:system_status")


@staff_member_required
def system_status_services(request):
    """HTMX-партиал блока «Сервисы» для автообновления."""
    return render(request, "dashboard/_services_block.html", {"services": infra_services.services_overview()})


@staff_member_required
def system_status(request):
    """Системный статус: Redis/Celery/PostgreSQL, расписание Beat, хвост errors.log.
    Только чтение.
    """
    context = {
        "page_title": "Системный статус — DOPX Staff",
        "active_tab": "system_status",
        "status": infra_services.system_status_overview(),
    }
    return render(request, "dashboard/system_status.html", context)
