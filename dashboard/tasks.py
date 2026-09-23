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

import re

from celery import shared_task
from django.utils import timezone

# 2026-09-22: парсим "Готово: проверено N, ..." (verify_names_with_ai.py,
# style.SUCCESS-строка в конце handle()) — единственный сигнал "чанк
# уткнулся в свой --limit, возможно есть ещё кандидаты" без отдельного
# API/поля прогресса. Если формат вывода команды когда-то изменится и
# регулярка перестанет матчиться — chunking просто тихо остановится после
# первого чанка (see checked_this_chunk is None ниже), не зависнет молча.
_CHECKED_RE = re.compile(r"проверено (\d+)", re.IGNORECASE)
# Предохранитель на случай гипотетического бага в дедупликации команды
# (кандидат почему-то никогда не исключается из следующего чанка) — без
# этого потенциальный бесконечный цикл чанков. 50 чанков * 40 = 2000
# кандидатов, с большим запасом выше текущих ~914 сущностей в базе.
_MAX_CHUNKS = 50


@shared_task(bind=True, max_retries=0)
def run_management_command(self, run_id: str) -> None:
    """Выполняет ОДИН ранее провалидированный и сохранённый
    ManagementCommandRun (см. dashboard/command_runner.py::trigger_command —
    args там уже собраны и провалидированы ДО постановки в очередь, здесь
    просто call_command(*positional, **kwargs) по сохранённому снимку).

    2026-09-22, прямая просьба пользователя после нескольких зависаний
    verify_names_with_ai --all --limit 0 (часы ОДНИМ синхронным процессом —
    OOM/SIGKILL форкнутого дочернего процесса воркера убивал прогон
    целиком без возможности продолжить, статус навсегда оставался
    RUNNING — полный разбор в истории чата): для команд с
    CommandSpec.auto_chunk_limit каждый вызов таска обрабатывает только
    ОДНУ короткую порцию, а не весь объём — и, если похоже, что кандидаты
    ещё остались, сам ставит в очередь следующую порцию (apply_async).
    Одна и та же ManagementCommandRun-запись живёт все чанки — status
    остаётся RUNNING, stdout/stderr дописываются, celery_task_id каждый
    раз обновляется на актуальный (чтобы "Стоп" останавливал именно
    текущую/следующую порцию, а не первую)."""
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

    # "Стоп" мог сработать МЕЖДУ чанками — следующая порция уже стояла в
    # очереди (countdown=5) до того, как staff нажал кнопку. Проверяем: раз
    # scripts_revoke_run уже выставил FAILED, просто не выполняем эту порцию.
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
        # timezone.now() при USE_TZ=True (dopx/settings.py) возвращает
        # время в UTC, а не в TIME_ZONE="Asia/Almaty" — без localtime() в
        # заголовке чанка показывалось бы время на 5 часов меньше
        # реального (было замечено пользователем: "09:19" вместо
        # фактических ~14:19).
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
