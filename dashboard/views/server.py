# dashboard/views/server.py
"""Раздел «Сервер»: процессы, сайт, кэш и очереди, база, логи, бэкапы, режим обслуживания. Только суперпользователь."""
from __future__ import annotations

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.http import FileResponse, Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from .. import infra_services, server_tools
from ..audit import log_staff_action
from ..models import AuditAction

TABS = (("overview", "Обзор", "ti-server-2"), ("logs", "Логи", "ti-file-text"), ("db", "База данных", "ti-database"))


def _superuser_only(request):
    if not request.user.is_superuser:
        raise Http404


@staff_member_required
def server_view(request):
    _superuser_only(request)
    tab = request.GET.get("tab", "overview")
    tab = tab if tab in {t[0] for t in TABS} else "overview"
    context = {"page_title": "Сервер — DOPX Staff", "active_tab": "server", "tab": tab, "tabs": TABS}
    if tab == "overview":
        context.update(services=infra_services.services_overview(), cache=server_tools.cache_overview(),
                       backups=server_tools.backups_overview(), maintenance=server_tools.maintenance_enabled(),
                       release=infra_services.release_info())
    elif tab == "logs":
        logs = server_tools.list_logs()
        name = request.GET.get("log") or (logs[0]["name"] if logs else "")
        lines = min(int(request.GET.get("lines", 300) or 300), 2000)
        context.update(logs=logs, log_name=name, log_query=request.GET.get("q", "").strip(),
                       log_level=request.GET.get("level", ""), log_lines=lines, levels=server_tools.LOG_LEVELS,
                       line_options=(100, 300, 1000, 2000),
                       log=server_tools.read_log(name, lines, request.GET.get("q", "").strip(), request.GET.get("level", ""))
                       if name else None)
    else:
        context["db"] = server_tools.db_overview()
    return render(request, "dashboard/server.html", context)


@staff_member_required
def server_log_download(request):
    _superuser_only(request)
    path = server_tools.log_path(request.GET.get("log", ""))
    if path is None:
        raise Http404
    return FileResponse(open(path, "rb"), as_attachment=True, filename=path.name)


@staff_member_required
@require_POST
def server_action(request):
    """Все кнопки раздела: одно действие — одна запись в аудите."""
    _superuser_only(request)
    action = request.POST.get("action", "")
    tab = "overview"
    try:
        if action == "reload_web":
            ok, message = server_tools.reload_web()
        elif action == "clear_cache":
            ok, message = True, server_tools.clear_cache(request.POST.get("kind", ""))
        elif action == "purge_queue":
            name = request.POST.get("queue", "")
            ok, message = True, f"Очередь «{name}» очищена: удалено задач — {server_tools.purge_queue(name)}."
        elif action == "clear_sessions":
            ok, message = True, f"Удалено просроченных сессий: {server_tools.clear_expired_sessions()}."
        elif action == "maintenance":
            on = request.POST.get("on") == "1"
            server_tools.set_maintenance(on)
            ok, message = True, ("Режим обслуживания включён: посетители видят заглушку, сотрудники работают как обычно."
                                 if on else "Режим обслуживания выключен: сайт открыт для всех.")
        elif action == "cancel_query":
            tab = "db"
            ok = server_tools.cancel_query(int(request.POST.get("pid", 0)))
            message = "Запрос прерван." if ok else "Запрос уже завершился."
        elif action == "vacuum":
            tab = "db"
            table = request.POST.get("table", "")
            server_tools.vacuum_table(table)
            ok, message = True, f"Таблица {table} очищена от мёртвых строк, статистика обновлена."
        else:
            ok, message = False, "Неизвестное действие."
    except (ValueError, TypeError):
        ok, message = False, "Неверные параметры действия."
    except Exception as exc:  # noqa: BLE001 — показываем ошибку сотруднику, а не 500
        ok, message = False, f"Не получилось: {exc}"
    (messages.success if ok else messages.error)(request, message)
    log_staff_action(request, AuditAction.SERVER_ACTION, target=action,
                     details={k: v for k, v in request.POST.items() if k != "csrfmiddlewaretoken"} | {"ok": ok, "message": message})
    return redirect(f"{reverse('dashboard:server')}?tab={tab}")
