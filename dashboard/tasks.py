# dashboard/tasks.py
"""
Celery-таск для раздела "Скрипты и команды" (dashboard/command_runner.py::
trigger_command). ЯВНО отдельный модуль `dashboard/tasks.py` — 'dashboard'
сам по себе приложение в INSTALLED_APPS (в отличие от parsers.sportmonks,
см. подробный докстринг dopx/celery.py про баг с автодискавери
вложенных пакетов) — `app.autodiscover_tasks()` без доп. аргументов
находит его как обычно, отдельная регистрация не нужна.
"""
from __future__ import annotations

from celery import shared_task
from django.utils import timezone


@shared_task(bind=True, max_retries=0)
def run_management_command(self, run_id: str) -> None:
    """Выполняет ОДИН ранее провалидированный и сохранённый
    ManagementCommandRun (см. dashboard/command_runner.py::trigger_command —
    args там уже собраны и провалидированы ДО постановки в очередь, здесь
    просто call_command(*positional, **kwargs) по сохранённому снимку)."""
    from .command_runner import run_command_sync
    from .commands_registry import get_command
    from .models import ManagementCommandRun

    try:
        run = ManagementCommandRun.objects.get(id=run_id)
    except ManagementCommandRun.DoesNotExist:
        return

    spec = get_command(run.command_name)
    if spec is None:
        run.status = ManagementCommandRun.Status.FAILED
        run.stderr = f"Команда «{run.command_name}» больше не зарегистрирована в COMMAND_REGISTRY"
        run.finished_at = timezone.now()
        run.save(update_fields=["status", "stderr", "finished_at"])
        return

    run.status = ManagementCommandRun.Status.RUNNING
    run.started_at = timezone.now()
    run.celery_task_id = self.request.id or run.celery_task_id
    run.save(update_fields=["status", "started_at", "celery_task_id"])

    positional = run.args.get("positional", [])
    kwargs = run.args.get("kwargs", {})
    success, out, err = run_command_sync(spec, positional, kwargs)

    run.status = ManagementCommandRun.Status.SUCCESS if success else ManagementCommandRun.Status.FAILED
    run.stdout, run.stderr = out, err
    run.finished_at = timezone.now()
    run.save(update_fields=["status", "stdout", "stderr", "finished_at"])
