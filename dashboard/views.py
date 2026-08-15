# dashboard/views.py
"""
Staff-only дашборд. Тонкие вьюхи — вся агрегация в `services.py`.
`staff_member_required` (встроенный django.contrib.admin декоратор) —
редиректит на `/admin/login/` неавторизованных/не-staff, тот же контракт
доступа, что уже используется для `/api/docs/` и т.п. (см. dopx/urls.py).
"""
from __future__ import annotations

import csv

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from matches.models import Match
from users.models import SuspiciousActivityFlag

from . import infra_services, parser_tools, services
from .audit import log_staff_action
from .models import AuditAction, StaffActionLog

# Пресеты диапазона для overview — используются и во вьюхе, и в шаблоне
# (кнопки переключения), единый источник правды на оба конца.
OVERVIEW_DAY_PRESETS = [7, 14, 30, 90]


@staff_member_required
def overview(request):
    try:
        days = int(request.GET.get("days", 14))
    except (TypeError, ValueError):
        days = 14
    days = days if days in OVERVIEW_DAY_PRESETS else 14

    context = {
        "page_title": "Обзор — DOPX Staff",
        "active_tab": "overview",
        "metrics": services.overview_metrics(days=days),
        "selected_days": days,
        "day_presets": OVERVIEW_DAY_PRESETS,
    }
    return render(request, "dashboard/overview.html", context)


@staff_member_required
def traffic(request):
    try:
        days = int(request.GET.get("days", 14))
    except (TypeError, ValueError):
        days = 14
    days = days if days in OVERVIEW_DAY_PRESETS else 14

    context = {
        "page_title": "Трафик — DOPX Staff",
        "active_tab": "traffic",
        "traffic": services.traffic_summary(days=days),
        "selected_days": days,
        "day_presets": OVERVIEW_DAY_PRESETS,
    }
    return render(request, "dashboard/traffic.html", context)


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
@require_POST
def data_health_resync_match(request, match_id):
    """Кнопка «Досинхронизировать» у конкретного матча в data-health —
    синхронный full-ресинк (см. dashboard/parser_tools.py::resync_match),
    не ждём celery beat. Подходит для точечного случая (1-2 проблемных
    матча); для массового резинка используется задача sync_kff_premier_league
    из вкладки «Парсер» ниже."""
    match = get_object_or_404(Match, id=match_id)
    success, message = parser_tools.resync_match(match)
    (messages.success if success else messages.error)(request, message)
    log_staff_action(
        request, AuditAction.MATCH_RESYNC,
        target=str(match), details={"match_id": str(match.id), "success": success, "message": message},
    )
    return redirect("dashboard:data_health")


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
    """Одна кнопка = одно решение. Логика 1:1 с `users/admin.py::
    SuspiciousActivityFlagAdmin.mark_confirmed/mark_dismissed` — тот же
    контракт (status + reviewed_by + reviewed_at), просто без захода в
    Django admin ради разового триажа."""
    flag = get_object_or_404(SuspiciousActivityFlag, id=flag_id)
    action = request.POST.get("action")

    if action not in ("confirm", "dismiss"):
        messages.error(request, "Неизвестное действие")
        return redirect("dashboard:antifraud")

    flag.status = "confirmed" if action == "confirm" else "dismissed"
    flag.reviewed_by = request.user
    flag.reviewed_at = timezone.now()
    flag.save(update_fields=["status", "reviewed_by", "reviewed_at", "updated_at"])

    messages.success(
        request,
        f"Флаг {'подтверждён' if action == 'confirm' else 'отклонён'}: {flag.user.username}",
    )
    log_staff_action(
        request,
        AuditAction.ANTIFRAUD_FLAG_CONFIRMED if action == "confirm" else AuditAction.ANTIFRAUD_FLAG_DISMISSED,
        target=flag.user.username,
        details={"flag_id": str(flag.id), "score": flag.score, "source": flag.source},
    )
    return redirect("dashboard:antifraud")


@staff_member_required
def antifraud_export_csv(request):
    """Выгрузка текущей очереди (флаги + диспуты) в CSV — нужно для разбора
    вне браузера (Excel/Google Sheets) или передачи саппорту/юристам без
    доступа в Django admin."""
    queue = services.antifraud_queue(limit=1000)

    response = HttpResponse(content_type="text/csv")
    filename = f"antifraud_queue_{timezone.now():%Y%m%d_%H%M}.csv"
    response["Content-Disposition"] = f'attachment; filename="{filename}"'

    writer = csv.writer(response)
    writer.writerow(["Тип", "ID", "Пользователь", "Источник/тема", "Создано"])
    for flag in queue["pending_flags"]:
        writer.writerow(["Флаг", flag.id, flag.user.username, flag.get_source_display(), flag.created_at.isoformat()])
    for dispute in queue["pending_disputes"]:
        writer.writerow([
            "Диспут",
            dispute.id,
            dispute.user.username if dispute.user else dispute.contact_email,
            dispute.subject,
            dispute.created_at.isoformat(),
        ])

    return response


# ============================================================
# Парсер-тулинг: сырые ответы KFF, ручной ресинк, ручной запуск задач
# ============================================================

@staff_member_required
def parser_tools_view(request):
    """Единая страница инструментов парсера (задача #91/#92/#93, расширено
    задачей "польза для решения проблем проекта" — добавлены поиск матча,
    live-проверка KFF API и инспекция очереди celery):
      - поиск матча по названию команд/external_id → UUID и быстрые ссылки;
      - форма просмотра сырого JSON от KFF API по external_id + эндпоинту;
      - живая (синхронная) проверка доступности внешнего KFF API;
      - список активных/зарезервированных celery-задач с revoke;
      - кнопки ручного запуска celery-задач синка (с дебаунсом).
    Ресинк конкретного матча живёт на вкладке data-health (там есть список
    матчей под рукой), сюда вынесены только "безадресные" инструменты."""
    raw_result = None
    raw_form = {
        "external_id": request.GET.get("external_id", ""),
        "endpoint": request.GET.get("endpoint", "events"),
    }
    if raw_form["external_id"]:
        try:
            ext_id = int(raw_form["external_id"])
        except ValueError:
            messages.error(request, "external_id должен быть числом")
        else:
            raw_result = parser_tools.raw_kff_response(ext_id, raw_form["endpoint"])
            log_staff_action(
                request, AuditAction.RAW_KFF_LOOKUP,
                target=f"external_id={ext_id} endpoint={raw_form['endpoint']}",
                details={"external_id": ext_id, "endpoint": raw_form["endpoint"], "error": raw_result.get("error")},
            )

    # Поиск матча — не логируем в аудит (read-only просмотр, тот же
    # уровень чувствительности, что и обычный список в Django admin).
    search_query = request.GET.get("q", "").strip()
    search_results = parser_tools.search_matches(search_query) if search_query else []

    context = {
        "page_title": "Парсер — DOPX Staff",
        "active_tab": "parser_tools",
        "raw_endpoints": parser_tools.RAW_ENDPOINTS,
        "raw_form": raw_form,
        "raw_result": raw_result,
        "triggerable_tasks": parser_tools.TRIGGERABLE_TASKS,
        "search_query": search_query,
        "search_results": search_results,
        "celery_tasks": parser_tools.list_active_celery_tasks(),
    }
    return render(request, "dashboard/parser_tools.html", context)


@staff_member_required
@require_POST
def parser_trigger_task(request):
    task_name = request.POST.get("task_name", "")
    success, message = parser_tools.trigger_task(task_name)
    (messages.success if success else messages.warning)(request, message)
    log_staff_action(
        request, AuditAction.CELERY_TASK_TRIGGERED,
        target=task_name, details={"success": success, "message": message},
    )
    return redirect("dashboard:parser_tools")


@staff_member_required
@require_POST
def parser_kff_health_check(request):
    """Живая проверка "это мы или у них API лежит" — синхронный вызов,
    результат сразу в messages, без ожидания celery-цикла и без захода в
    логи сервера."""
    result = parser_tools.kff_api_health_check()
    if result["ok"]:
        messages.success(request, f"KFF API доступен ({result['elapsed_ms']}мс)")
    else:
        messages.error(request, f"KFF API недоступен: {result['status']} ({result['elapsed_ms']}мс)")
    log_staff_action(
        request, AuditAction.KFF_HEALTH_CHECK,
        target="KFF API", details=result,
    )
    return redirect("dashboard:parser_tools")


@staff_member_required
@require_POST
def parser_revoke_task(request, task_id):
    """Отзыв/остановка конкретной celery-задачи по id (из таблицы "Активные
    задачи" на этой же странице). terminate=True — только по явному чекбоксу
    в форме, см. комментарий в parser_tools.py::revoke_celery_task про риск
    SIGKILL посреди транзакции."""
    terminate = request.POST.get("terminate") == "1"
    success, message = parser_tools.revoke_celery_task(task_id, terminate=terminate)
    (messages.success if success else messages.error)(request, message)
    log_staff_action(
        request, AuditAction.CELERY_TASK_REVOKED,
        target=task_id, details={"success": success, "message": message, "terminate": terminate},
    )
    return redirect("dashboard:parser_tools")


@staff_member_required
def audit_log(request):
    """Вкладка «Аудит» — журнал кастомных staff-экшенов (StaffActionLog).
    ОБЫЧНЫЕ CRUD-изменения через Django admin (add/change/delete любой
    модели) сюда НЕ попадают — они уже логируются самим Django в
    django_admin_log (LogEntry), см. /admin/ → "История" у любого объекта."""
    entries = list(StaffActionLog.objects.select_related("actor")[:200])
    context = {
        "page_title": "Аудит — DOPX Staff",
        "active_tab": "audit",
        "entries": entries,
    }
    return render(request, "dashboard/audit_log.html", context)
