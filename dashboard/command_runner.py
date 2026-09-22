# dashboard/command_runner.py
"""
Валидация/сборка аргументов из POST-формы (dashboard/commands_registry.py::
ArgSpec) в позиционные/именованные аргументы call_command(), плюс запуск
самой команды — синхронно для readonly-диагностики (staff должен сразу
увидеть отчёт, без лишнего Celery-хопа на дешёвой read-only операции) и
асинхронно через Celery (dashboard/tasks.py::run_management_command) для
всего остального, что реально может занять больше пары секунд.

Никакого произвольного текста командной строки от staff не принимается —
только значения под уже описанные в COMMAND_REGISTRY поля конкретной
команды (dashboard/commands_registry.py — тот же принцип allowlist'а, что
и у dashboard/parser_tools.py::TRIGGERABLE_TASKS для celery-задач).
"""
from __future__ import annotations

import io
import logging
from typing import Any

from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from .commands_registry import ArgSpec, CommandSpec, get_command

logger = logging.getLogger(__name__)


class ValidationError(Exception):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


def _coerce_value(arg: ArgSpec, raw: str) -> Any:
    raw = (raw or "").strip()
    if arg.kind == "int":
        return int(raw)
    if arg.kind == "float":
        return float(raw)
    if arg.kind == "choice":
        if raw not in arg.choices:
            raise ValueError(f"недопустимое значение {raw!r}, ожидалось одно из {arg.choices}")
        return raw
    return raw  # "str"


def build_command_args(spec: CommandSpec, post_data, apply: bool = False) -> tuple[list, dict]:
    """post_data — dict-подобный объект (request.POST), поля названы по
    ArgSpec.dest (для list_str — многострочный textarea с тем же именем).
    Возвращает (positional_args, kwargs) готовые для call_command(spec.name,
    *positional_args, **kwargs). Бросает ValidationError со списком человеко-
    читаемых сообщений, если что-то не проходит валидацию — форма
    перерисовывается с этими сообщениями, call_command вообще не вызывается."""
    positional: list[Any] = []
    kwargs: dict[str, Any] = {}
    errors: list[str] = []

    for arg in spec.args:
        if arg.kind == "flag":
            value = post_data.get(arg.dest) in ("on", "1", "true", "True")
            if not arg.positional:
                kwargs[arg.dest] = value
            continue

        if arg.kind == "list_str":
            raw_block = post_data.get(arg.dest, "") or ""
            values = [line.strip() for line in raw_block.replace(",", "\n").splitlines() if line.strip()]
            if arg.required and not values:
                errors.append(f"«{arg.help or arg.dest}» — обязательное поле, укажите хотя бы одно значение")
                continue
            if arg.positional:
                positional.extend(values)
            elif values:
                kwargs[arg.dest] = values
            continue

        raw = post_data.get(arg.dest, "")
        if not (raw or "").strip():
            if arg.required:
                errors.append(f"«{arg.help or arg.dest}» — обязательное поле")
            elif arg.positional:
                pass  # опциональный позиционный (nargs='?') — просто не передаём
            continue

        try:
            value = _coerce_value(arg, raw)
        except ValueError as e:
            errors.append(f"«{arg.help or arg.dest}»: {e}")
            continue

        if arg.positional:
            positional.append(value)
        else:
            kwargs[arg.dest] = value

    if spec.has_apply_flag:
        kwargs["apply"] = apply

    if errors:
        raise ValidationError(errors)

    return positional, kwargs


def run_command_sync(spec: CommandSpec, positional: list, kwargs: dict) -> tuple[bool, str, str]:
    """Прямой вызов call_command() в текущем процессе — используется для
    readonly-диагностики (быстро, staff должен увидеть отчёт немедленно) и
    воркером run_management_command для остальных категорий. Возвращает
    (success, stdout, stderr) — CommandError/любое исключение ловим сами и
    кладём текст в stderr, а не роняем вызывающий код (staff должен увидеть
    ПОЧЕМУ команда упала, а не голый 500)."""
    out, err = io.StringIO(), io.StringIO()
    try:
        call_command(spec.name, *positional, stdout=out, stderr=err, **kwargs)
        success = True
    except CommandError as e:
        err.write(f"\nCommandError: {e}")
        success = False
    except Exception as e:
        logger.error("run_command_sync(%s): %s", spec.name, e, exc_info=True)
        err.write(f"\n{type(e).__name__}: {e}")
        success = False
    return success, out.getvalue(), err.getvalue()


def trigger_command(request, command_name: str, apply: bool = False) -> tuple[bool, str, "ManagementCommandRun | None"]:  # noqa: F821
    """Точка входа из view (scripts_trigger). Валидирует POST против
    COMMAND_REGISTRY, создаёт ManagementCommandRun (PENDING), и либо
    выполняет её сразу (readonly), либо ставит в очередь Celery
    (dashboard/tasks.py::run_management_command)."""
    from .models import ManagementCommandRun

    spec = get_command(command_name)
    if spec is None:
        return False, f"Неизвестная команда: {command_name}", None

    try:
        positional, kwargs = build_command_args(spec, request.POST, apply=apply)
    except ValidationError as e:
        return False, "Ошибка в параметрах: " + "; ".join(e.errors), None

    run = ManagementCommandRun.objects.create(
        command_name=spec.name,
        args={"positional": positional, "kwargs": kwargs},
        status=ManagementCommandRun.Status.PENDING,
        triggered_by=request.user if request.user.is_authenticated else None,
        triggered_by_username=request.user.username if request.user.is_authenticated else "",
    )

    if spec.danger == "readonly":
        # Синхронно — дёшево, и staff должен увидеть отчёт диагностики
        # сразу на той же странице, без лишнего похода через очередь.
        run.status = ManagementCommandRun.Status.RUNNING
        run.started_at = timezone.now()
        run.save(update_fields=["status", "started_at"])
        success, out, err = run_command_sync(spec, positional, kwargs)
        run.status = ManagementCommandRun.Status.SUCCESS if success else ManagementCommandRun.Status.FAILED
        run.stdout, run.stderr = out, err
        run.finished_at = timezone.now()
        run.save(update_fields=["status", "stdout", "stderr", "finished_at"])
        message = f"«{spec.label}» выполнена" if success else f"«{spec.label}» завершилась с ошибкой"
        return success, message, run

    from .tasks import run_management_command

    async_result = run_management_command.delay(str(run.id))
    run.celery_task_id = async_result.id
    run.save(update_fields=["celery_task_id"])
    return True, f"«{spec.label}» поставлена в очередь", run
