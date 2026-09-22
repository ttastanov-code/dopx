# parsers/tasks.py
"""
2026-09-09: KFF-парсер и вся его инфраструктура (client.py, importers.py,
pipeline.py, photo_scraper.py, все Celery-задачи синхронизации матчей отсюда)
физически удалены по явному решению пользователя — Sportmonks остаётся
ЕДИНСТВЕННЫМ источником данных матчей (см. parsers/sportmonks/tasks.py).

Здесь остаётся только check_sync_errors_and_alert — она НЕ была привязана к
конкретному источнику: читает Match/MatchEvent общими полями (status,
created_at, has_lineup), не знает и не спрашивает, кто именно записал эти
строки (KFF или Sportmonks — см. ParserSyncRun.source, parsers/models.py).
Поэтому это единственная задача из старого файла, которую можно было
оставить как есть, не переписывая заново под Sportmonks.

ВАЖНОЕ ПОСЛЕДСТВИЕ УДАЛЕНИЯ (зафиксировано, чтобы не потерялось): вместе с
parsers/kff/importers.py::import_match_core ушла и единственная реализация
детектора ParserDiscrepancy (правка счёта/статуса задним числом поверх уже
завершённого матча, см. её докстринг в parsers/models.py) — на стороне
Sportmonks-импортёра (parsers/sportmonks/importers.py) аналогичного детектора
НЕТ. Карточка "Расхождения импорта" на /staff/dashboard/data-health/
продолжит работать (сам ParserDiscrepancy.objects.filter(reviewed=False) в
dashboard/services.py ничего не знает про источник), но новых записей в неё
писать больше некому. Если нужно закрыть этот пробел — это отдельная,
самостоятельная задача (добавить эквивалентную проверку в
parsers/sportmonks/importers.py::import_match_core), не восстановление
удалённого кода.

Аналогично: `Match.was_rescheduled` (см. её докстринг в matches/models.py)
раньше проставлялся `_detect_rescheduled_outlier` внутри KFF-шной
update_match_statuses — тоже удалено вместе с задачей. Поле на модели
осталось (историческая разметка уже импортированных матчей не трогается),
но новые переносы дат Sportmonks-эпохи этим способом больше не ловятся.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.html import strip_tags

logger = logging.getLogger(__name__)


@shared_task
def check_sync_errors_and_alert():
    """Проверка ошибок синхронизации за последние 24 часа и алерт при
    необходимости. Источник-агностично: считает по Match/MatchEvent, не
    важно, кто их создал (Sportmonks — единственный активный синк, но поле
    ParserSyncRun.source в принципе позволяет иметь несколько)."""
    from matches.models import Match

    now = timezone.now()
    cutoff = now - timedelta(hours=24)

    # ИСПРАВЛЕНО (2026-09-10, расследование алерта "12 матчей без составов
    # за 24ч" — жалоба пользователя "не работает парсер или че"): матчи с
    # Match.decided_administratively=True (неявка/техническое поражение/
    # прерван и засчитан, см. её докстринг в matches/models.py) у
    # Sportmonks НИКОГДА не будут иметь lineups/events — состав/события
    # неоткуда взять для матча, который по факту не доигрывался в обычном
    # режиме. Без этого исключения такие матчи инфлировали счётчик как
    # будто это сбой синхронизации, хотя это ожидаемая характеристика
    # результата. Поле появилось только 2026-09-10 — на старых Match-
    # записях (импортированных до этой правки) оно останется False, даже
    # если матч на самом деле техническое поражение; разовая коррекция для
    # уже накопленных записей — повторный прогон daily sportmonks_sync_
    # season (parsers/sportmonks/tasks.py) сам переустановит его при
    # следующем обновлении фикстуры, специальная management-команда не
    # нужна (все фикстуры сезона синкаются каждую ночь в 03:30).
    matches_without_lineups = Match.objects.filter(
        status="finished",
        created_at__gte=cutoff,
        has_lineup=False,
        decided_administratively=False,
    ).count()

    from events.models import MatchEvent

    matches_without_events = (
        Match.objects.filter(
            status="finished", created_at__gte=cutoff, decided_administratively=False
        )
        .exclude(
            id__in=MatchEvent.objects.filter(created_at__gte=cutoff).values_list(
                "match_id", flat=True
            )
        )
        .count()
    )

    threshold_lineups = 5
    threshold_events = 10

    alerts = []

    if matches_without_lineups > threshold_lineups:
        alerts.append(f"⚠️ {matches_without_lineups} матчей без составов за 24ч")

    if matches_without_events > threshold_events:
        alerts.append(f"⚠️ {matches_without_events} матчей без событий за 24ч")

    if alerts:
        error_msg = "Проблемы с синхронизацией:\n" + "\n".join(alerts)
        logger.warning(error_msg)
        _send_sync_error_alert(
            error_msg,
            "sync_monitoring",
            extra_data={
                "matches_without_lineups": matches_without_lineups,
                "matches_without_events": matches_without_events,
            },
        )
        return {"status": "alert_sent", "alerts": alerts}

    logger.info("✅ Sync monitoring: No critical issues detected")
    return {"status": "ok"}


@shared_task(bind=True, max_retries=0)
def verify_names_with_ai_monthly(self):
    """Ежемесячный автопрогон «Проверка ФИО (ИИ)» (2026-09-22, прямая
    просьба пользователя: "надо раз в месяц даже сделать", после того как
    сделали разовый ручной прогон по всей базе через дашборд).

    НЕ `--all` — обычный режим команды (parsers/management/commands/
    verify_names_with_ai.py) и так сам проверяет только НОВОЕ:
    name_source=guessed_transliteration (свежепришедшие через трансферы/
    новые сезоны игроки, угаданные транслитерацией) плюс записи с прошлым
    check_failed (см. дедупликацию в самой команде — 2026-09-22 фикс,
    исключающий check_failed из "уже проверено"). Уже одобренные/
    отклонённые staff записи не трогает. --all запускался ОДИН раз вручную
    для разового прохода по уже существующей базе — сюда его сознательно
    не добавляем, иначе каждый месяц заново тратились бы вызовы на давно
    подтверждённые записи. `limit=100` — защитный потолок на случай
    аномального наплыва новых записей за месяц (обычный трансферный поток
    КПЛ таким лимитом даже близко не исчерпывается), не даёт задаче
    случайно улететь в сотни вызовов без присмотра.

    Пишет ManagementCommandRun как обычный ручной запуск (тот же
    dashboard/command_runner.py::run_command_sync) — результат виден в
    "Скрипты и команды" → История запусков, с triggered_by_username=
    "celery-beat (ежемесячно)" вместо логина staff, чтобы сразу было
    видно, что запуск автоматический. log_staff_action сюда НЕ пишем —
    это не действие staff (нет request/user), тот же принцип, что и у
    остальных периодических задач этого файла/parsers.sportmonks.tasks —
    видимость через ParserSyncRun/ManagementCommandRun, не через
    StaffActionLog."""
    from dashboard.command_runner import run_command_sync
    from dashboard.commands_registry import get_command
    from dashboard.models import ManagementCommandRun

    spec = get_command("verify_names_with_ai")
    if spec is None:
        logger.error("verify_names_with_ai_monthly: команда verify_names_with_ai не найдена в COMMAND_REGISTRY")
        return {"status": "error", "reason": "command_not_registered"}

    positional: list = []
    kwargs = {"entity": None, "limit": 100, "delay": 4.0, "recheck": False, "dry_run": False}

    run = ManagementCommandRun.objects.create(
        command_name=spec.name,
        args={"positional": positional, "kwargs": kwargs},
        status=ManagementCommandRun.Status.RUNNING,
        triggered_by=None,
        triggered_by_username="celery-beat (ежемесячно)",
        started_at=timezone.now(),
        celery_task_id=self.request.id or "",
    )

    success, out, err = run_command_sync(spec, positional, kwargs)

    run.status = ManagementCommandRun.Status.SUCCESS if success else ManagementCommandRun.Status.FAILED
    run.stdout, run.stderr = out, err
    run.finished_at = timezone.now()
    run.save(update_fields=["status", "stdout", "stderr", "finished_at"])

    return {"status": "success" if success else "failed", "run_id": str(run.id)}


def _send_sync_error_alert(error_message: str, alert_type: str, extra_data: dict = None):
    """Отправка email-алерта админу при критических ошибках синка."""
    if not getattr(settings, "ENABLE_SYNC_ERROR_ALERTS", True):
        return

    admin_email = getattr(settings, "ADMIN_ALERT_EMAIL", settings.CONTACT_EMAIL)
    site_url = getattr(settings, "SITE_URL", "https://dopx.kz")

    subject = f"DOPX Sync Alert [{alert_type}]"

    html_message = render_to_string(
        "emails/sync_error_alert.html",
        {
            "error_message": error_message,
            "alert_type": alert_type,
            "extra_data": extra_data,
            "timestamp": timezone.now(),
            "site_url": site_url,
        },
    )

    try:
        send_mail(
            subject=subject,
            message=strip_tags(html_message),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[admin_email],
            html_message=html_message,
            fail_silently=True,
        )
        logger.info(f"✅ Sync error alert sent to {admin_email}")
    except Exception as e:
        logger.error(f"❌ Failed to send sync error alert: {e}")
