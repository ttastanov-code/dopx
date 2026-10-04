# dashboard/views/parser.py
"""Инструменты парсера и синк Sportmonks."""
from __future__ import annotations

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.decorators import user_passes_test
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.models import PlatformSetting, get_setting
from parsers.sportmonks.client import get_request_counts

from .. import parser_tools
from ..audit import log_staff_action
from ..models import AuditAction


# ============================================================
# Инструменты парсера: поиск матча, ресинк, ручной запуск задач
# ============================================================

@staff_member_required
def parser_tools_view(request):
    """Страница инструментов парсера: поиск матча, проверка API,
    очередь celery с отзывом задач, ручной запуск задач синка.
    """
    # Поиск в аудит не пишем.
    search_query = request.GET.get("q", "").strip()
    search_year_param = request.GET.get("year", "")
    search_year = int(search_year_param) if search_year_param.isdigit() else None
    search = (
        parser_tools.search_matches(search_query, year=search_year) if search_query
        else {"results": [], "total_count": 0, "year": search_year or timezone.now().year}
    )

    context = {
        "page_title": "Парсер — DOPX Staff",
        "active_tab": "parser_tools",
        "triggerable_tasks": parser_tools.TRIGGERABLE_TASKS,
        "task_descriptions": parser_tools.TASK_DESCRIPTIONS,
        "sportmonks_triggerable_tasks": parser_tools.SPORTMONKS_TRIGGERABLE_TASKS,
        "sportmonks_task_descriptions": parser_tools.SPORTMONKS_TASK_DESCRIPTIONS,
        "search_query": search_query,
        "search_results": search["results"],
        "search_total_count": search["total_count"],
        "search_year": search["year"],
        "search_available_years": parser_tools.available_search_years(),
        "celery_tasks": parser_tools.list_active_celery_tasks(),
        "sportmonks_health": parser_tools.get_cached_sportmonks_health(),
        # Счётчик запросов к API читаем напрямую из кэша при каждой загрузке.
        "sportmonks_request_counts": get_request_counts(),
        # Рубильник синка — через get_setting(), как и сами задачи.
        "sportmonks_sync_enabled": get_setting("sportmonks_sync_enabled", True),
    }
    return render(request, "dashboard/parser_tools.html", context)


@staff_member_required
def parser_tasks_partial(request):
    """Карточка очереди celery для HTMX-поллинга."""
    context = {"celery_tasks": parser_tools.list_active_celery_tasks()}
    return render(request, "dashboard/_celery_tasks_card.html", context)


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
def parser_sportmonks_health_check(request):
    """Синхронная проверка доступности Sportmonks API.
    HTMX-запрос получает результат прямо в карточку, обычный POST — messages + redirect.
    """
    result = parser_tools.sportmonks_api_health_check()
    log_staff_action(
        request, AuditAction.SPORTMONKS_HEALTH_CHECK,
        target="Sportmonks API", details=result,
    )
    if request.headers.get("HX-Request") == "true":
        return render(request, "dashboard/_sportmonks_health_result.html", {"sportmonks_health": result})

    if result["ok"]:
        messages.success(request, f"Sportmonks API доступен: {result['status']} ({result['elapsed_ms']}мс)")
    else:
        messages.error(request, f"Sportmonks API недоступен: {result['status']} ({result['elapsed_ms']}мс)")
    return redirect("dashboard:parser_tools")


SPORTMONKS_SYNC_ENABLED_KEY = "sportmonks_sync_enabled"


@staff_member_required
@user_passes_test(lambda u: u.is_superuser)
@require_POST
def sportmonks_sync_toggle(request):
    """Включить/выключить синк с Sportmonks (только суперпользователь).

    PlatformSetting sportmonks_sync_enabled проверяется в начале каждой задачи синка,
    перезапуск воркеров не нужен (кэш настроек — 60 с). Не влияет на
    sportmonks_sync_coach_activity и ручную проверку доступности API.
    """
    setting, created = PlatformSetting.objects.get_or_create(
        key=SPORTMONKS_SYNC_ENABLED_KEY,
        defaults={
            "value": "true",
            "value_type": PlatformSetting.TYPE_BOOL,
            "description": "Главный рубильник синка с Sportmonks API (все 6 celery-задач). "
                            "Выключите, если кончилась подписка/квота — синк встанет на паузу "
                            "без перезапуска сервисов, задачи продолжат тикать по расписанию, "
                            "но не будут дёргать API.",
            "updated_by": request.user,
        },
    )
    was_enabled = True if created else setting.typed_value()
    new_value = not was_enabled
    setting.value = "true" if new_value else "false"
    setting.value_type = PlatformSetting.TYPE_BOOL
    setting.updated_by = request.user
    setting.save(update_fields=["value", "value_type", "updated_by", "updated_at"])

    from django.core.cache import cache
    cache.delete(f"platform_setting:{SPORTMONKS_SYNC_ENABLED_KEY}")

    if new_value:
        messages.success(request, "Синк с Sportmonks включён — задачи возобновят обращения к API в течение минуты")
    else:
        messages.warning(request, "Синк с Sportmonks выключен — задачи будут пропускаться без обращений к API")
    log_staff_action(
        request, AuditAction.SPORTMONKS_SYNC_TOGGLED,
        target="Sportmonks", details={"before": was_enabled, "after": new_value},
    )
    return redirect("dashboard:parser_tools")


@staff_member_required
@require_POST
def parser_revoke_task(request, task_id):
    """Отзыв celery-задачи. terminate=True — только по явному чекбоксу."""
    terminate = request.POST.get("terminate") == "1"
    success, message = parser_tools.revoke_celery_task(task_id, terminate=terminate)
    (messages.success if success else messages.error)(request, message)
    log_staff_action(
        request, AuditAction.CELERY_TASK_REVOKED,
        target=task_id, details={"success": success, "message": message, "terminate": terminate},
    )
    return redirect("dashboard:parser_tools")
