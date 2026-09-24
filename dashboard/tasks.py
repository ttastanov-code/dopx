# dashboard/tasks.py
"""Celery-задача для «Скриптов и команд»."""
from __future__ import annotations

import re

from celery import shared_task
from django.utils import timezone

# Строка «Готово: проверено N, ...» из verify_names_with_ai — признак, что могли остаться кандидаты.
# Не распарсилась — чанкинг останавливается после первой порции.
_CHECKED_RE = re.compile(r"проверено (\d+)", re.IGNORECASE)
# Предохранитель от бесконечного цикла чанков.
_MAX_CHUNKS = 50


@shared_task(bind=True, max_retries=0)
def run_management_command(self, run_id: str) -> None:
    """Выполняет сохранённый ManagementCommandRun (аргументы уже провалидированы).
    Для команд с auto_chunk_limit — одна порция за вызов, следующую ставит сама;
    запись одна на все порции, stdout/stderr дописываются.
    """
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

    # Запуск остановили между порциями — эту не выполняем.
    if run.status == ManagementCommandRun.Status.FAILED:
        return

    positional = run.args.get("positional", [])
    kwargs = dict(run.args.get("kwargs", {}))

    chunked = spec.auto_chunk_limit is not None and not kwargs.get("dry_run")
    if chunked:
        kwargs["limit"] = spec.auto_chunk_limit

    if run.started_at is None:
        run.started_at = timezone.now()
    run.status = ManagementCommandRun.Status.RUNNING
    run.celery_task_id = self.request.id or run.celery_task_id
    run.save(update_fields=["status", "started_at", "celery_task_id"])

    success, out, err = run_command_sync(spec, positional, kwargs)

    chunk_index = run.args.get("_chunk_index", 0) + 1
    if chunked:
        # localtime() — время в Asia/Almaty, а не UTC.
        header = f"\n=== чанк {chunk_index} (порция ≤{spec.auto_chunk_limit}) — {timezone.localtime():%Y-%m-%d %H:%M:%S} ===\n"
        run.stdout = (run.stdout or "") + header + out
        run.stderr = (run.stderr or "") + (header + err if err else "")
    else:
        run.stdout, run.stderr = out, err

    checked_this_chunk = None
    if chunked:
        m = _CHECKED_RE.search(out)
        if m:
            checked_this_chunk = int(m.group(1))

    more_work = (
        chunked
        and success
        and checked_this_chunk is not None
        and checked_this_chunk >= spec.auto_chunk_limit
        and chunk_index < _MAX_CHUNKS
    )

    if more_work:
        new_args = dict(run.args)
        new_args["_chunk_index"] = chunk_index
        run.args = new_args
        run.save(update_fields=["stdout", "stderr", "args"])
        next_result = run_management_command.apply_async(args=[str(run.id)], countdown=5)
        ManagementCommandRun.objects.filter(id=run.id).update(celery_task_id=next_result.id)
        return

    if chunked and chunk_index >= _MAX_CHUNKS and success:
        run.stderr = (run.stderr or "") + (
            f"\n⚠️ Достигнут предохранитель {_MAX_CHUNKS} чанков — прогон остановлен на всякий случай "
            f"(возможно, ещё остались непроверенные кандидаты). Запустите команду ещё раз — дедупликация "
            f"сама продолжит с того места, где остановились."
        )

    run.status = ManagementCommandRun.Status.SUCCESS if success else ManagementCommandRun.Status.FAILED
    run.finished_at = timezone.now()
    run.save(update_fields=["status", "stdout", "stderr", "finished_at"])
