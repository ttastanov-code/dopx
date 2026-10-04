# dashboard/views/scripts.py
"""Запуск management-команд из дашборда."""
from __future__ import annotations

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .. import command_runner, commands_registry, parser_tools
from ..audit import log_staff_action
from ..models import AuditAction, ManagementCommandRun, MANUAL_STOP_NOTE


# ============================================================
# Скрипты и команды: запуск management-команд из дашборда.
# См. commands_registry.py и command_runner.py.
# ============================================================

SCRIPTS_RUNS_PAGE_SIZE = 15


def _scripts_runs_page(request):
    """История запусков с пагинацией — общая для страницы и HTMX-поллинга."""
    qs = ManagementCommandRun.objects.select_related("triggered_by").order_by("-created_at")
    page = Paginator(qs, SCRIPTS_RUNS_PAGE_SIZE).get_page(request.GET.get("page"))
    # Поллим только пока есть незавершённые запуски в системе.
    page.has_active = ManagementCommandRun.objects.filter(
        status__in=[ManagementCommandRun.Status.PENDING, ManagementCommandRun.Status.RUNNING]
    ).exists()
    return page


@staff_member_required
def scripts_view(request):
    """Раздел «Скрипты и команды»: команды по категориям + история запусков."""
    context = {
        "page_title": "Скрипты и команды — DOPX Staff",
        "active_tab": "scripts",
        "command_categories": commands_registry.categories(),
        "recent_runs": _scripts_runs_page(request),
    }
    return render(request, "dashboard/scripts.html", context)


@staff_member_required
def scripts_runs_partial(request):
    """Таблица запусков для HTMX-поллинга. ?page= сохраняется между тиками."""
    context = {"recent_runs": _scripts_runs_page(request)}
    return render(request, "dashboard/_scripts_runs_table.html", context)


@staff_member_required
@require_POST
def scripts_trigger(request):
    """Запуск команды из COMMAND_REGISTRY. Без чекбокса apply опасные команды
    делают dry-run. Для cleanup_load_test нужно вписать имя команды.
    """
    command_name = request.POST.get("command_name", "")
    spec = commands_registry.get_command(command_name)
    if spec is None:
        messages.error(request, f"Неизвестная команда: {command_name}")
        return redirect("dashboard:scripts")

    apply = request.POST.get("apply") == "on"
    # cleanup_load_test удаляет всегда, остальные — только с apply.
    if spec.name in commands_registry.CONFIRM_TEXT_COMMANDS and (apply or not spec.has_apply_flag):
        confirm_text = request.POST.get("confirm_text", "").strip()
        if confirm_text != spec.name:
            messages.error(
                request,
                f"Для «{spec.label}» нужно вписать имя команды («{spec.name}») в поле подтверждения — не совпало.",
            )
            return redirect("dashboard:scripts")

    success, message, run = command_runner.trigger_command(request, command_name, apply=apply)
    (messages.success if success else messages.warning)(request, message)
    log_staff_action(
        request, AuditAction.MANAGEMENT_COMMAND_TRIGGERED,
        target=command_name,
        details={"success": success, "message": message, "apply": apply, "run_id": str(run.id) if run else None},
    )
    return redirect("dashboard:scripts")


@staff_member_required
@require_POST
def scripts_revoke_run(request, run_id):
    """Остановить запущенную команду. Команда крутится в синхронном цикле внутри
    celery-задачи, поэтому только terminate=True (SIGTERM).
    """
    run = get_object_or_404(ManagementCommandRun, id=run_id)

    if run.status not in (ManagementCommandRun.Status.PENDING, ManagementCommandRun.Status.RUNNING):
        messages.info(request, f"«{run.command_name}» уже завершена ({run.get_status_display()}) — останавливать нечего.")
        return redirect("dashboard:scripts")

    if not run.celery_task_id:
        messages.error(request, f"У запуска «{run.command_name}» нет celery_task_id — нечего отзывать.")
        return redirect("dashboard:scripts")

    success, message = parser_tools.revoke_celery_task(run.celery_task_id, terminate=True)
    if success:
        run.status = ManagementCommandRun.Status.FAILED
        run.stderr = (run.stderr + "\n" if run.stderr else "") + MANUAL_STOP_NOTE
        run.finished_at = timezone.now()
        run.save(update_fields=["status", "stderr", "finished_at"])
    (messages.success if success else messages.error)(request, message)
    log_staff_action(
        request, AuditAction.CELERY_TASK_REVOKED,
        target=run.command_name,
        details={"success": success, "message": message, "run_id": str(run.id), "terminate": True},
    )
    return redirect("dashboard:scripts")
