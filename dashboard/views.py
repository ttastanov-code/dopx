# dashboard/views.py
"""Staff-дашборд. Вьюхи тонкие, агрегация — в services.py."""
from __future__ import annotations

import csv

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import user_passes_test
from django.core.paginator import Paginator
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from django.utils.dateparse import parse_datetime

from core.admin_actions import _csv_safe
from core.models import NAME_SOURCE_AI_VERIFIED, PlatformSetting, get_setting
from evaluations.models import EvaluationSession
from matches.models import Match
from notifications.models import ContactSubmission
from parsers import name_ai
from parsers.models import ConfirmedNameCorrection, NameVerificationSuggestion, ParserDiscrepancy
from parsers.sportmonks.client import get_request_counts
from players.models import Player, PotentialDuplicatePlayer
from players.services import merge_players
from seasons.models import Season
from users.models import SuspiciousActivityFlag

from . import command_runner, commands_registry, infra_services, parser_tools, services
from .audit import log_staff_action
from .models import AuditAction, ManagementCommandRun, StaffActionLog

# Пресеты периода для обзора (вьюха + кнопки в шаблоне).
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


# ============================================================
# Матчи: ручная правка и пересчёт агрегатов
# ============================================================

MATCHES_PAGE_SIZE = 25


@staff_member_required
def matches_list(request):
    search = request.GET.get("q", "").strip()
    status = request.GET.get("status", "")
    season_id = request.GET.get("season", "")
    qs = services.matches_queryset(search=search, status=status, season_id=season_id)
    matches_page = Paginator(qs, MATCHES_PAGE_SIZE).get_page(request.GET.get("page"))
    context = {
        "page_title": "Матчи — DOPX Staff",
        "active_tab": "matches",
        "matches": matches_page,
        "search": search,
        "status": status,
        "season_id": season_id,
        "status_choices": Match.STATUS_CHOICES,
        "seasons": Season.objects.select_related("league").order_by("-year"),
    }
    return render(request, "dashboard/matches_list.html", context)


@staff_member_required
def match_detail(request, match_id):
    match = get_object_or_404(
        Match.objects.select_related(
            "league", "season", "home_team", "away_team", "home_coach", "away_coach", "referee"
        ),
        id=match_id,
    )

    if request.method == "POST":
        before = {
            "status": match.status, "home_score": match.home_score, "away_score": match.away_score,
            "start_time": match.start_time.isoformat() if match.start_time else None,
            "tour": match.tour, "manual_override": match.manual_override,
        }
        errors: list[str] = []

        new_status = request.POST.get("status", match.status)
        if new_status not in dict(Match.STATUS_CHOICES):
            errors.append("Недопустимый статус")
        else:
            match.status = new_status

        for field in ("home_score", "away_score", "tour"):
            raw = request.POST.get(field, "").strip()
            if raw == "":
                setattr(match, field, None)
            else:
                try:
                    setattr(match, field, int(raw))
                except ValueError:
                    errors.append(f"«{field}» — должно быть целым числом")

        start_time_raw = request.POST.get("start_time", "").strip()
        if start_time_raw:
            parsed = parse_datetime(start_time_raw)
            if parsed is None:
                errors.append("Некорректный формат времени начала (ожидается ГГГГ-ММ-ДДTЧЧ:ММ)")
            else:
                match.start_time = timezone.make_aware(parsed) if timezone.is_naive(parsed) else parsed

        # Снятый чекбокс не приходит в POST.
        match.manual_override = request.POST.get("manual_override") in ("on", "1", "true")

        if errors:
            for err in errors:
                messages.error(request, err)
        else:
            match.save(update_fields=[
                "status", "home_score", "away_score", "tour", "start_time", "manual_override", "updated_at",
            ])
            messages.success(request, "Матч обновлён. Не забудьте «Пересчитать», если поменялся счёт/статус.")
            log_staff_action(
                request, AuditAction.MATCH_MANUAL_EDIT,
                target=str(match),
                details={"match_id": str(match.id), "before": before, "after": {
                    "status": match.status, "home_score": match.home_score, "away_score": match.away_score,
                    "start_time": match.start_time.isoformat() if match.start_time else None,
                    "tour": match.tour, "manual_override": match.manual_override,
                }},
            )
        return redirect("dashboard:match_detail", match_id=match.id)

    context = {
        "page_title": f"{match} — DOPX Staff",
        "active_tab": "matches",
        "match": match,
        "status_choices": Match.STATUS_CHOICES,
    }
    return render(request, "dashboard/match_detail.html", context)


@staff_member_required
@require_POST
def match_trigger_recalc(request, match_id):
    match = get_object_or_404(Match, id=match_id)
    from aggregates.tasks import recalculate_all_aggregates_for_match
    recalculate_all_aggregates_for_match.delay(str(match.id))
    messages.success(request, "Пересчёт агрегатов запущен в фоне — обновится в течение минуты.")
    log_staff_action(
        request, AuditAction.MATCH_RECALC_TRIGGERED,
        target=str(match), details={"match_id": str(match.id)},
    )
    return redirect("dashboard:match_detail", match_id=match.id)


# ============================================================
# Настройки платформы (PlatformSetting). Секреты сюда не кладём.
# ============================================================

@staff_member_required
def platform_settings(request):
    context = {
        "page_title": "Настройки платформы — DOPX Staff",
        "active_tab": "platform_settings",
        "settings_list": PlatformSetting.objects.select_related("updated_by").order_by("key"),
        "type_choices": PlatformSetting.TYPE_CHOICES,
    }
    return render(request, "dashboard/platform_settings.html", context)


@staff_member_required
@require_POST
def platform_settings_create(request):
    key = request.POST.get("key", "").strip()
    if not key:
        messages.error(request, "Ключ не может быть пустым")
        return redirect("dashboard:platform_settings")
    if PlatformSetting.objects.filter(key=key).exists():
        messages.error(request, f"Настройка «{key}» уже существует")
        return redirect("dashboard:platform_settings")

    value_type = request.POST.get("value_type", PlatformSetting.TYPE_STRING)
    if value_type not in dict(PlatformSetting.TYPE_CHOICES):
        value_type = PlatformSetting.TYPE_STRING

    setting = PlatformSetting.objects.create(
        key=key,
        value=request.POST.get("value", "").strip(),
        value_type=value_type,
        description=request.POST.get("description", "").strip(),
        updated_by=request.user,
    )
    from django.core.cache import cache
    cache.delete(f"platform_setting:{key}")

    messages.success(request, f"Настройка «{key}» создана")
    log_staff_action(
        request, AuditAction.PLATFORM_SETTING_CREATED,
        target=key, details={"value": setting.value, "value_type": setting.value_type},
    )
    return redirect("dashboard:platform_settings")


@staff_member_required
@require_POST
def platform_settings_update(request, key):
    setting = get_object_or_404(PlatformSetting, key=key)
    before_value = setting.value

    value_type = request.POST.get("value_type", setting.value_type)
    if value_type in dict(PlatformSetting.TYPE_CHOICES):
        setting.value_type = value_type
    setting.value = request.POST.get("value", "").strip()
    setting.description = request.POST.get("description", "").strip()
    setting.updated_by = request.user
    setting.save(update_fields=["value", "value_type", "description", "updated_by", "updated_at"])

    from django.core.cache import cache
    cache.delete(f"platform_setting:{key}")

    messages.success(request, f"«{key}» обновлена — новое значение применится в течение минуты (кэш)")
    log_staff_action(
        request, AuditAction.PLATFORM_SETTING_CHANGED,
        target=key, details={"before": before_value, "after": setting.value, "value_type": setting.value_type},
    )
    return redirect("dashboard:platform_settings")


@staff_member_required
@require_POST
def platform_settings_delete(request, key):
    setting = get_object_or_404(PlatformSetting, key=key)
    setting.delete()

    from django.core.cache import cache
    cache.delete(f"platform_setting:{key}")

    messages.success(request, f"«{key}» удалена")
    log_staff_action(request, AuditAction.PLATFORM_SETTING_DELETED, target=key)
    return redirect("dashboard:platform_settings")


# ============================================================
# Пользователи. Действия обратимые (is_active=False, без удаления).
# ============================================================

USERS_PAGE_SIZE = 25


@staff_member_required
def users_list(request):
    search = request.GET.get("q", "").strip()
    qs = services.users_queryset(search=search)
    # Размер страницы — из настроек платформы (dashboard_users_page_size), по умолчанию 25.
    page_size = get_setting("dashboard_users_page_size", USERS_PAGE_SIZE)
    users_page = Paginator(qs, page_size).get_page(request.GET.get("page"))
    context = {
        "page_title": "Пользователи — DOPX Staff",
        "active_tab": "users",
        "users": users_page,
        "search": search,
    }
    return render(request, "dashboard/users_list.html", context)


@staff_member_required
def user_detail(request, user_id):
    User = get_user_model()
    user_obj = get_object_or_404(User.objects.select_related("xp"), id=user_id)
    context = {
        "page_title": f"{user_obj.username} — DOPX Staff",
        "active_tab": "users",
        "user_obj": user_obj,
        **services.user_detail_context(user_obj),
    }
    return render(request, "dashboard/user_detail.html", context)


@staff_member_required
@require_POST
def user_toggle_ban(request, user_id):
    User = get_user_model()
    user_obj = get_object_or_404(User, id=user_id)
    # Нельзя забанить самого себя.
    if user_obj.id == request.user.id:
        messages.error(request, "Нельзя заблокировать самого себя")
        return redirect("dashboard:user_detail", user_id=user_obj.id)

    user_obj.is_active = not user_obj.is_active
    user_obj.save(update_fields=["is_active"])

    if user_obj.is_active:
        messages.success(request, f"{user_obj.username} разблокирован")
        log_staff_action(request, AuditAction.USER_UNBANNED, target=user_obj.username, details={"user_id": str(user_obj.id)})
    else:
        messages.success(request, f"{user_obj.username} заблокирован — вход в аккаунт закрыт")
        log_staff_action(request, AuditAction.USER_BANNED, target=user_obj.username, details={"user_id": str(user_obj.id)})
    return redirect("dashboard:user_detail", user_id=user_obj.id)


@staff_member_required
@require_POST
def user_reset_trust_score(request, user_id):
    User = get_user_model()
    user_obj = get_object_or_404(User, id=user_id)
    before = user_obj.trust_score
    user_obj.trust_score = 1.0
    user_obj.save(update_fields=["trust_score"])
    messages.success(request, f"Оценка доверия {user_obj.username} сброшена на 1.0 (была {before:.2f})")
    log_staff_action(
        request, AuditAction.USER_TRUST_SCORE_RESET,
        target=user_obj.username, details={"user_id": str(user_obj.id), "before": before, "after": 1.0},
    )
    return redirect("dashboard:user_detail", user_id=user_obj.id)


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
def data_health_partial(request):
    """Содержимое data_health без обёртки — для HTMX-поллинга."""
    context = {
        "health": services.data_health_summary(),
        "infra": infra_services.infra_health(),
    }
    return render(request, "dashboard/_data_health_content.html", context)


def _resolve_match_for_resync(match_id: str) -> Match | None:
    """Ищет матч по UUID, затем по sportmonks_id (старые записи ошибок синка)."""
    import uuid as uuid_module

    match = None
    try:
        match = Match.objects.filter(id=uuid_module.UUID(str(match_id))).first()
    except (ValueError, TypeError, AttributeError):
        pass
    if match is None:
        match = Match.objects.filter(sportmonks_id=str(match_id)).first()
    return match


@staff_member_required
@require_POST
def data_health_resync_match(request, match_id):
    """Синхронный полный ресинк одного матча из data-health.
    match_id — UUID или sportmonks_id (старые записи в error_samples).
    """
    match = _resolve_match_for_resync(match_id)
    if match is None:
        messages.error(request, f"Матч с id={match_id} не найден (возможно, устаревшая запись об ошибке).")
        return redirect("dashboard:data_health")

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
    """Подтвердить/отклонить флаг — та же логика, что в админке."""
    flag = get_object_or_404(SuspiciousActivityFlag, id=flag_id)
    action = request.POST.get("action")

    if action not in ("confirm", "dismiss"):
        messages.error(request, "Неизвестное действие")
        return redirect("dashboard:antifraud")

    flag.status = "confirmed" if action == "confirm" else "dismissed"
    flag.reviewed_by = request.user
    flag.reviewed_at = timezone.now()
    flag.save(update_fields=["status", "reviewed_by", "reviewed_at", "updated_at"])

    if action == "dismiss":
        # «Отклонить» снимает авто-поправку рейтинга.
        from aggregates.tasks import apply_divergence_dismissal

        apply_divergence_dismissal([flag])

    # У entity-сигналов (vote_spike и т.п.) user пустой — цель это content_object.
    flag_target = flag.user.username if flag.user else str(flag.content_object or flag.get_source_display())

    messages.success(
        request,
        f"Флаг {'подтверждён' if action == 'confirm' else 'отклонён'}: {flag_target}",
    )
    log_staff_action(
        request,
        AuditAction.ANTIFRAUD_FLAG_CONFIRMED if action == "confirm" else AuditAction.ANTIFRAUD_FLAG_DISMISSED,
        target=flag_target,
        details={"flag_id": str(flag.id), "score": flag.score, "source": flag.source},
    )
    return redirect("dashboard:antifraud")


@staff_member_required
def antifraud_export_csv(request):
    """Выгрузка очереди антифрода (флаги + диспуты) в CSV."""
    queue = services.antifraud_queue(limit=1000)

    response = HttpResponse(content_type="text/csv")
    filename = f"antifraud_queue_{timezone.now():%Y%m%d_%H%M}.csv"
    response["Content-Disposition"] = f'attachment; filename="{filename}"'

    writer = csv.writer(response)
    writer.writerow(["Тип", "ID", "Пользователь", "Источник/тема", "Создано"])
    for flag in queue["pending_flags"]:
        # У entity-сигналов user пустой.
        who = flag.user.username if flag.user else f"[сущность] {flag.content_object or '—'}"
        # _csv_safe — защита от formula injection.
        writer.writerow([_csv_safe(v) for v in (
            "Флаг", flag.id, who, flag.get_source_display(), flag.created_at.isoformat(),
        )])
    for dispute in queue["pending_disputes"]:
        writer.writerow([_csv_safe(v) for v in (
            "Диспут",
            dispute.id,
            dispute.user.username if dispute.user else dispute.contact_email,
            dispute.subject,
            dispute.created_at.isoformat(),
        )])

    return response


# ============================================================
# Центр доверия к данным
# ============================================================

@staff_member_required
def data_trust(request):
    report_status = request.GET.get("report_status", "open")
    if report_status not in services.DATA_TRUST_REPORT_STATUS_FILTERS:
        report_status = "open"
    context = {
        "page_title": "Центр доверия к данным — DOPX Staff",
        "active_tab": "data_trust",
        "trust": services.data_trust_summary(report_status=report_status),
    }
    return render(request, "dashboard/data_trust.html", context)


@staff_member_required
@require_POST
def data_trust_resolve_report(request, submission_id):
    """Закрыть жалобу на данные матча. status: 'resolved' (по умолчанию) или 'closed'."""
    submission = get_object_or_404(ContactSubmission, id=submission_id, category="data_error")
    new_status = request.POST.get("status", "resolved")
    if new_status not in ("resolved", "closed"):
        new_status = "resolved"
    submission.status = new_status
    submission.save(update_fields=["status", "updated_at"])
    messages.success(request, f"Жалоба «{submission.subject}» закрыта")
    log_staff_action(
        request, AuditAction.DATA_ERROR_REPORT_RESOLVED,
        target=submission.subject,
        details={
            "submission_id": str(submission.id),
            "match_id": str(submission.related_match_id) if submission.related_match_id else None,
            "status": new_status,
        },
    )
    return redirect(f"{reverse('dashboard:data_trust')}?report_status={request.POST.get('return_status', 'open')}")


@staff_member_required
@require_POST
def data_trust_review_discrepancy(request, discrepancy_id):
    """Отметить расхождение импорта как разобранное."""
    discrepancy = get_object_or_404(ParserDiscrepancy, id=discrepancy_id)
    discrepancy.reviewed = True
    discrepancy.reviewed_by = request.user
    discrepancy.reviewed_at = timezone.now()
    discrepancy.save(update_fields=["reviewed", "reviewed_by", "reviewed_at"])
    messages.success(request, f"Расхождение по «{discrepancy.match_label}» отмечено разобранным")
    log_staff_action(
        request, AuditAction.PARSER_DISCREPANCY_REVIEWED,
        target=discrepancy.match_label,
        details={"discrepancy_id": str(discrepancy.id), "field_name": discrepancy.field_name},
    )
    return redirect("dashboard:data_trust")


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


# ============================================================
# Скрипты и команды: запуск management-команд из дашборда.
# См. commands_registry.py и command_runner.py.
# ============================================================

SCRIPTS_RUNS_PAGE_SIZE = 15


def _scripts_runs_page(request):
    """История запусков с пагинацией — общая для страницы и HTMX-поллинга."""
    qs = ManagementCommandRun.objects.select_related("triggered_by").order_by("-created_at")
    return Paginator(qs, SCRIPTS_RUNS_PAGE_SIZE).get_page(request.GET.get("page"))


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

    if spec.name == "cleanup_load_test":
        confirm_text = request.POST.get("confirm_text", "").strip()
        if confirm_text != spec.name:
            messages.error(
                request,
                f"Для «{spec.label}» нужно вписать имя команды («{spec.name}») в поле подтверждения — не совпало.",
            )
            return redirect("dashboard:scripts")

    apply = request.POST.get("apply") == "on"
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
        run.stderr = (run.stderr + "\n" if run.stderr else "") + "Остановлено вручную staff (terminate)."
        run.finished_at = timezone.now()
        run.save(update_fields=["status", "stderr", "finished_at"])
    (messages.success if success else messages.error)(request, message)
    log_staff_action(
        request, AuditAction.CELERY_TASK_REVOKED,
        target=run.command_name,
        details={"success": success, "message": message, "run_id": str(run.id), "terminate": True},
    )
    return redirect("dashboard:scripts")


# ============================================================
# Проверка ФИО (ИИ): ручное подтверждение предложений Gemini.
# ============================================================

def _names_review_queue_context() -> dict:
    """Живая часть очереди (ждут проверки / ошибки Gemini) для страницы и HTMX-поллинга.
    «Недавно разобранные» с пагинацией сюда не входят.
    """
    pending = NameVerificationSuggestion.objects.filter(status="pending_review").order_by("-created_at")
    # Для массового подтверждения — только где Gemini согласен с текущим написанием.
    matches_count = pending.filter(matches_current=True).count()
    # Честный общий счётчик ошибок до среза [:20].
    failed_qs = NameVerificationSuggestion.objects.filter(status="check_failed").order_by("-created_at")
    failed_count = failed_qs.count()
    failed = failed_qs[:20]
    return {
        "pending_suggestions": pending,
        "failed_suggestions": failed,
        "failed_count": failed_count,
        "matches_count": matches_count,
    }


@staff_member_required
def names_review(request):
    # «Недавно разобранные» — постранично по 20.
    recent_decided_qs = (
        NameVerificationSuggestion.objects.filter(status__in=["approved", "rejected"])
        .select_related("reviewed_by").order_by("-reviewed_at")
    )
    recent_decided_paginator = Paginator(recent_decided_qs, 20)
    recent_decided = recent_decided_paginator.get_page(request.GET.get("page"))
    context = {
        "page_title": "Проверка ФИО (ИИ) — DOPX Staff",
        "active_tab": "names_review",
        **_names_review_queue_context(),
        "recent_decided": recent_decided,
        "gemini_configured": name_ai.is_configured(),
    }
    return render(request, "dashboard/names_review.html", context)


@staff_member_required
def names_review_partial(request):
    """Карточки очереди для HTMX-поллинга (раз в 6 с — внутри есть поля ввода)."""
    return render(request, "dashboard/_names_review_queue.html", _names_review_queue_context())


@staff_member_required
@require_POST
def names_review_bulk_confirm_matches(request):
    """Массово подтверждает только те предложения, где Gemini сказал, что текущее
    написание верное. Реальные исправления разбираются по одному.
    name_source всегда становится ai_verified.
    """
    qs = NameVerificationSuggestion.objects.filter(status="pending_review", matches_current=True)
    confirmed = 0
    skipped = 0
    for suggestion in qs:
        entity = suggestion.content_object
        if entity is None:
            suggestion.status = "rejected"
            suggestion.reviewed_by = request.user
            suggestion.reviewed_at = timezone.now()
            suggestion.save(update_fields=["status", "reviewed_by", "reviewed_at", "updated_at"])
            skipped += 1
            continue

        final_first = (suggestion.suggested_first_name or entity.first_name).strip()
        final_last = (suggestion.suggested_last_name or entity.last_name).strip()
        update_fields = []
        if final_first and final_first != entity.first_name:
            ConfirmedNameCorrection.objects.update_or_create(
                wrong_text=entity.first_name.strip().lower(),
                defaults={"correct_text": final_first, "source_suggestion": suggestion, "created_by": request.user},
            )
            entity.first_name = final_first
            update_fields.append("first_name")
        if final_last and final_last != entity.last_name:
            ConfirmedNameCorrection.objects.update_or_create(
                wrong_text=entity.last_name.strip().lower(),
                defaults={"correct_text": final_last, "source_suggestion": suggestion, "created_by": request.user},
            )
            entity.last_name = final_last
            update_fields.append("last_name")

        entity.name_source = NAME_SOURCE_AI_VERIFIED
        update_fields.append("name_source")
        entity.save(update_fields=update_fields + ["updated_at"])

        suggestion.status = "approved"
        suggestion.reviewed_by = request.user
        suggestion.reviewed_at = timezone.now()
        suggestion.save(update_fields=["status", "reviewed_by", "reviewed_at", "updated_at"])
        confirmed += 1

    log_staff_action(
        request, AuditAction.NAME_SUGGESTION_APPROVED,
        target="bulk_confirm_matches",
        details={"confirmed": confirmed, "skipped_deleted_entity": skipped},
    )
    suffix = f", пропущено (сущность удалена): {skipped}" if skipped else ""
    messages.success(request, f"Массово подтверждено: {confirmed}{suffix}")
    return redirect("dashboard:names_review")


@staff_member_required
@require_POST
def names_review_action(request, suggestion_id):
    """approve — пишет ConfirmedNameCorrection и сразу обновляет сущность.
    reject/скрыть ошибку — только очередь, данные сайта не трогает.
    Имя берём из формы: staff мог поправить предложение Gemini.
    """
    suggestion = get_object_or_404(NameVerificationSuggestion, id=suggestion_id)
    action = request.POST.get("action")

    if action not in ("approve", "reject"):
        messages.error(request, f"Неизвестное действие: {action}")
        return redirect("dashboard:names_review")

    if suggestion.status not in ("pending_review", "check_failed"):
        messages.warning(request, "Это предложение уже разобрано.")
        return redirect("dashboard:names_review")

    if action == "reject":
        # «Скрыть» у технической ошибки Gemini удаляет запись, чтобы следующий
        # обычный прогон проверил сущность заново.
        if suggestion.status == "check_failed":
            target = f"{suggestion.entity_label}:{suggestion.object_id}"
            details = {
                "current": f"{suggestion.current_first_name} {suggestion.current_last_name}",
                "dismissed_error": suggestion.error_message,
            }
            suggestion.delete()
            log_staff_action(request, AuditAction.NAME_SUGGESTION_REJECTED, target=target, details=details)
            messages.success(request, "Ошибка скрыта — запись сама попадёт под проверку в следующем обычном прогоне (без --recheck).")
            return redirect("dashboard:names_review")

        suggestion.status = "rejected"
        suggestion.reviewed_by = request.user
        suggestion.reviewed_at = timezone.now()
        suggestion.save(update_fields=["status", "reviewed_by", "reviewed_at", "updated_at"])
        log_staff_action(
            request, AuditAction.NAME_SUGGESTION_REJECTED,
            target=f"{suggestion.entity_label}:{suggestion.object_id}",
            details={"current": f"{suggestion.current_first_name} {suggestion.current_last_name}"},
        )
        messages.success(request, "Предложение отклонено.")
        return redirect("dashboard:names_review")

    final_first = (request.POST.get("first_name") or suggestion.suggested_first_name or "").strip()
    final_last = (request.POST.get("last_name") or suggestion.suggested_last_name or "").strip()
    if not final_first and not final_last:
        messages.error(request, "Пустое имя и фамилия — нечего подтверждать.")
        return redirect("dashboard:names_review")

    entity = suggestion.content_object
    if entity is None:
        messages.error(request, "Сущность (игрок/судья/тренер) больше не существует — подтвердить нечего.")
        suggestion.status = "rejected"
        suggestion.reviewed_by = request.user
        suggestion.reviewed_at = timezone.now()
        suggestion.save(update_fields=["status", "reviewed_by", "reviewed_at", "updated_at"])
        return redirect("dashboard:names_review")

    old_first, old_last = entity.first_name, entity.last_name
    update_fields = []
    if final_first and final_first != entity.first_name:
        ConfirmedNameCorrection.objects.update_or_create(
            wrong_text=entity.first_name.strip().lower(),
            defaults={"correct_text": final_first, "source_suggestion": suggestion, "created_by": request.user},
        )
        entity.first_name = final_first
        update_fields.append("first_name")
    if final_last and final_last != entity.last_name:
        ConfirmedNameCorrection.objects.update_or_create(
            wrong_text=entity.last_name.strip().lower(),
            defaults={"correct_text": final_last, "source_suggestion": suggestion, "created_by": request.user},
        )
        entity.last_name = final_last
        update_fields.append("last_name")

    if update_fields:
        entity.name_source = NAME_SOURCE_AI_VERIFIED
        update_fields.append("name_source")
        entity.save(update_fields=update_fields + ["updated_at"])

    suggestion.status = "approved"
    suggestion.reviewed_by = request.user
    suggestion.reviewed_at = timezone.now()
    suggestion.save(update_fields=["status", "reviewed_by", "reviewed_at", "updated_at"])

    log_staff_action(
        request, AuditAction.NAME_SUGGESTION_APPROVED,
        target=f"{suggestion.entity_label}:{suggestion.object_id}",
        details={"was": f"{old_first} {old_last}", "now": f"{final_first} {final_last}"},
    )
    messages.success(request, f"Подтверждено: {old_first} {old_last} → {final_first} {final_last}")
    return redirect("dashboard:names_review")


def _player_dup_stats(player: Player) -> dict:
    """Сводка по дублям игроков для очереди."""
    return {
        "player": player,
        "appearances": player.matchlineupplayer_set.count(),
        "events_count": player.events.count(),
        "evaluations_count": player.player_evaluations.count(),
        "aggregates_count": player.match_aggregates.count(),
    }


@staff_member_required
def duplicate_players_review(request):
    """Очередь «Дубли игроков». Флаги ставит импорт при совпадении ФИО в команде.
    Слияние выполняется синхронно во вьюхе.
    """
    flags = list(
        PotentialDuplicatePlayer.objects.filter(reviewed=False)
        .select_related("existing_player__team", "new_player__team")
        .order_by("-created_at")
    )
    pairs = [
        {
            "flag": flag,
            "existing": _player_dup_stats(flag.existing_player),
            "new": _player_dup_stats(flag.new_player),
        }
        for flag in flags
    ]
    return render(request, "dashboard/duplicate_players_review.html", {
        "page_title": "Дубли игроков — DOPX Staff",
        "active_tab": "duplicate_players",
        "pairs": pairs,
        "pending_count": len(pairs),
    })


@staff_member_required
@require_POST
def duplicate_players_merge(request, flag_id):
    """keep=existing|new — какую запись оставить, вторая сливается и удаляется.
    HTMX получает пустой партиал на место карточки, обычный POST — редирект.
    """
    flag = get_object_or_404(PotentialDuplicatePlayer, id=flag_id)
    keep_side = request.POST.get("keep")
    if keep_side not in ("existing", "new"):
        messages.error(request, f"Неизвестное значение keep: {keep_side}")
        return redirect("dashboard:duplicate_players_review")

    if flag.reviewed:
        messages.warning(request, "Этот флаг уже разобран.")
        return redirect("dashboard:duplicate_players_review")

    keep, merge = (flag.existing_player, flag.new_player) if keep_side == "existing" else (flag.new_player, flag.existing_player)
    keep_name, keep_id = keep.full_name, keep.id
    merge_name, merge_id = merge.full_name, merge.id

    report = merge_players(keep, merge, apply=True)
    log_staff_action(
        request, AuditAction.DUPLICATE_PLAYERS_MERGED,
        target=f"player:{keep_id}",
        details={"kept": f"{keep_name} ({keep_id})", "merged": f"{merge_name} ({merge_id})", "report": report.lines},
    )

    if request.headers.get("HX-Request"):
        return render(request, "dashboard/_duplicate_players_resolved.html", {
            "message": f"Объединено: {merge_name} → {keep_name}",
        })
    messages.success(request, f"Объединено: {merge_name} → {keep_name}")
    return redirect("dashboard:duplicate_players_review")


@staff_member_required
@require_POST
def duplicate_players_dismiss(request, flag_id):
    """Не дубль (разные люди) — просто отмечаем флаг разобранным."""
    flag = get_object_or_404(PotentialDuplicatePlayer, id=flag_id)
    if flag.reviewed:
        messages.warning(request, "Этот флаг уже разобран.")
        return redirect("dashboard:duplicate_players_review")

    flag.reviewed = True
    flag.reviewed_by = request.user
    flag.reviewed_at = timezone.now()
    flag.note = "Отклонено вручную: разные люди."
    flag.save(update_fields=["reviewed", "reviewed_by", "reviewed_at", "note", "updated_at"])
    log_staff_action(
        request, AuditAction.DUPLICATE_PLAYER_FLAG_DISMISSED,
        target=f"player:{flag.existing_player_id}",
        details={"existing": str(flag.existing_player_id), "new": str(flag.new_player_id)},
    )

    if request.headers.get("HX-Request"):
        return render(request, "dashboard/_duplicate_players_resolved.html", {
            "message": "Отклонено — это разные люди.",
        })
    messages.success(request, "Отклонено — это разные люди.")
    return redirect("dashboard:duplicate_players_review")


# Реклама и виджеты: embed-виджеты и баннеры/рефералки на одной странице.

def _ads_stats_context() -> dict:
    """Статистика за период (виджеты, баннеры, рефералки) — для страницы и HTMX-поллинга."""
    from partners.selectors import (
        banner_totals,
        partner_referral_totals,
        top_banners,
        top_partners_by_referral_visits,
        top_widget_entities,
        widget_embed_totals,
    )
    from partners.models import Banner, Partner
    from players.models import Player
    from teams.models import Team

    # Период и размер топов — из настроек платформы.
    window_days = get_setting("ads_stats_window_days", 30)
    top_limit = get_setting("ads_top_items_limit", 10)

    top_players_raw = top_widget_entities("player", days=window_days, limit=top_limit)
    top_teams_raw = top_widget_entities("team", days=window_days, limit=top_limit)

    players_by_id = {
        str(p.id): p for p in Player.objects.filter(id__in=[r["entity_id"] for r in top_players_raw])
    }
    teams_by_id = {
        str(t.id): t for t in Team.objects.filter(id__in=[r["entity_id"] for r in top_teams_raw])
    }

    top_banners_raw = top_banners(days=window_days, limit=top_limit)
    banners_by_id = {
        str(b.id): b for b in Banner.objects.select_related("partner").filter(id__in=[r["banner_id"] for r in top_banners_raw])
    }

    top_partners_raw = top_partners_by_referral_visits(days=window_days, limit=top_limit)
    partners_by_slug = {
        p.slug: p for p in Partner.objects.filter(slug__in=[r["partner_slug"] for r in top_partners_raw])
    }

    return {
        "top_players": [
            {"entity": players_by_id[r["entity_id"]], "views": r["views"]}
            for r in top_players_raw if r["entity_id"] in players_by_id
        ],
        "top_teams": [
            {"entity": teams_by_id[r["entity_id"]], "views": r["views"]}
            for r in top_teams_raw if r["entity_id"] in teams_by_id
        ],
        "widget_totals": widget_embed_totals(days=window_days),
        "banner_totals": banner_totals(days=window_days),
        "top_banners": [
            {"banner": banners_by_id[r["banner_id"]], "impressions": r["impressions"], "clicks": r["clicks"], "ctr_percent": r["ctr_percent"]}
            for r in top_banners_raw if r["banner_id"] in banners_by_id
        ],
        "referral_visits_total": partner_referral_totals(days=window_days),
        "top_partners": [
            {"partner": partners_by_slug[r["partner_slug"]], "visits": r["visits"]}
            for r in top_partners_raw if r["partner_slug"] in partners_by_slug
        ],
    }


@staff_member_required
def ads(request):
    """/staff/dashboard/ads/. q_player/q_team — отдельный поиск игрока и команды
    для генератора embed-кода.
    """
    from core.utils import normalize_kz
    from players.models import Player
    from teams.models import Team

    q_player = request.GET.get("q_player", "").strip()
    q_team = request.GET.get("q_team", "").strip()
    player_id = request.GET.get("player_id", "")
    team_id = request.GET.get("team_id", "")

    # Поиск через normalize_kz (казахские буквы). Размер выдачи — из настроек.
    search_limit = get_setting("ads_search_results_limit", 10)

    if q_player:
        normalized_q = normalize_kz(q_player)
        player_results = [
            p for p in Player.objects.select_related("team").only("id", "first_name", "last_name", "team")
            if normalized_q in normalize_kz(f"{p.first_name} {p.last_name}")
        ][:search_limit]
    else:
        player_results = []

    if q_team:
        normalized_q = normalize_kz(q_team)
        team_results = [
            t for t in Team.objects.only("id", "name")
            if normalized_q in normalize_kz(t.name)
        ][:search_limit]
    else:
        team_results = []

    # Без поиска — превью на произвольном игроке/команде с данными.
    preview_player = None
    if player_id:
        preview_player = next((p for p in player_results if str(p.id) == player_id), None)
    if not preview_player:
        preview_player = player_results[0] if player_results else Player.objects.select_related("team").order_by("?").first()

    preview_team = None
    if team_id:
        preview_team = next((t for t in team_results if str(t.id) == team_id), None)
    if not preview_team:
        preview_team = team_results[0] if team_results else Team.objects.filter(is_active=True).order_by("?").first()

    def _embed_code(url: str, title: str, width: int = 320, height: int = 180) -> str:
        return (
            f'<iframe src="{url}" width="{width}" height="{height}" '
            f'style="border:none;border-radius:12px;overflow:hidden" title="{title}"></iframe>'
        )

    player_embed = None
    if preview_player:
        url = request.build_absolute_uri(reverse("players:widget", args=[preview_player.id]))
        player_embed = _embed_code(url, f"Рейтинг {preview_player.first_name} {preview_player.last_name} на DOPX")

    team_embed = None
    if preview_team:
        url = request.build_absolute_uri(reverse("teams:widget", args=[preview_team.id]))
        team_embed = _embed_code(url, f"Рейтинг {preview_team.name} на DOPX")

    standings_url = request.build_absolute_uri(reverse("core:standings_widget"))
    standings_embed = _embed_code(standings_url, "Турнирная таблица КПЛ на DOPX", width=340, height=360)

    # Виджет сборной сезона — всегда активный сезон.
    best_xi_url = request.build_absolute_uri(reverse("season_squad:widget"))
    best_xi_embed = _embed_code(best_xi_url, "Сборная DOPX сезона на DOPX", width=320, height=420)

    # Виджет лучших тура — активный сезон, последний завершённый тур.
    round_url = request.build_absolute_uri(reverse("round_squad:round_widget"))
    round_embed = _embed_code(round_url, "DOPX Лучшие тура", width=320, height=420)

    context = {
        "page_title": "Реклама и виджеты — DOPX Staff",
        "active_tab": "ads",
        "q_player": q_player,
        "q_team": q_team,
        "player_results": player_results,
        "team_results": team_results,
        "preview_player": preview_player,
        "preview_team": preview_team,
        "player_embed": player_embed,
        "team_embed": team_embed,
        "standings_embed": standings_embed,
        "best_xi_embed": best_xi_embed,
        "round_embed": round_embed,
        **_ads_stats_context(),
    }
    return render(request, "dashboard/ads.html", context)


@staff_member_required
def ads_stats_partial(request):
    """Блок статистики ads() для HTMX-поллинга (поиск и превью не обновляются)."""
    return render(request, "dashboard/_ads_stats_content.html", _ads_stats_context())


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
    counts = services.evaluation_session_delete_cascade(session)

    from aggregates.tasks import recalculate_all_aggregates_for_match
    recalculate_all_aggregates_for_match.delay(match_id)

    total_deleted = sum(v for k, v in counts.items() if k != "match_id")
    messages.success(
        request,
        f"Сессия «{username} — {match_str}» удалена вместе с {total_deleted} под-оценками. "
        f"Пересчёт агрегатов матча запущен в фоне.",
    )
    log_staff_action(
        request, AuditAction.EVALUATION_SESSION_DELETED,
        target=f"{username} — {match_str}",
        details={"user": username, "match_id": match_id, **counts},
    )
    return redirect("dashboard:evaluation_sessions_list")


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


# ============================================================
# Партнёры и баннеры (CRUD). Вход — со страницы «Реклама».
# ============================================================

PARTNERS_PAGE_SIZE = 30


@staff_member_required
def partners_list(request):
    from partners.models import PartnerType

    search = request.GET.get("q", "").strip()
    qs = services.partners_queryset(search=search)
    partners_page = Paginator(qs, PARTNERS_PAGE_SIZE).get_page(request.GET.get("page"))
    context = {
        "page_title": "Партнёры — DOPX Staff",
        "active_tab": "ads",
        "partners": partners_page,
        "search": search,
        "partner_type_choices": PartnerType.choices,
    }
    return render(request, "dashboard/partners_list.html", context)


@staff_member_required
@require_POST
def partner_create(request):
    from django.db import IntegrityError

    from partners.models import Partner, PartnerType

    name = request.POST.get("name", "").strip()
    slug = request.POST.get("slug", "").strip()
    partner_type = request.POST.get("partner_type", "").strip()

    if not name or not slug:
        messages.error(request, "Название и слаг обязательны.")
        return redirect("dashboard:partners_list")
    if partner_type not in dict(PartnerType.choices):
        messages.error(request, "Некорректный тип партнёра.")
        return redirect("dashboard:partners_list")

    try:
        partner = Partner.objects.create(
            name=name, slug=slug, partner_type=partner_type,
            contact_name=request.POST.get("contact_name", "").strip(),
            contact_email=request.POST.get("contact_email", "").strip(),
            website=request.POST.get("website", "").strip(),
            notes=request.POST.get("notes", "").strip(),
            is_active=request.POST.get("is_active") in ("on", "1", "true"),
        )
    except IntegrityError:
        messages.error(request, f"Слаг «{slug}» уже занят другим партнёром.")
        return redirect("dashboard:partners_list")

    messages.success(request, f"Партнёр «{partner.name}» создан.")
    log_staff_action(
        request, AuditAction.PARTNER_CREATED,
        target=partner.name, details={"partner_id": str(partner.id), "slug": partner.slug},
    )
    return redirect("dashboard:partner_detail", partner_id=partner.id)


@staff_member_required
def partner_detail(request, partner_id):
    from partners.models import Partner, PartnerType

    partner = get_object_or_404(Partner, id=partner_id)

    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        partner_type = request.POST.get("partner_type", "").strip()
        if not name:
            messages.error(request, "Название обязательно.")
        elif partner_type not in dict(PartnerType.choices):
            messages.error(request, "Некорректный тип партнёра.")
        else:
            before_active = partner.is_active
            partner.name = name
            partner.partner_type = partner_type
            partner.contact_name = request.POST.get("contact_name", "").strip()
            partner.contact_email = request.POST.get("contact_email", "").strip()
            partner.website = request.POST.get("website", "").strip()
            partner.notes = request.POST.get("notes", "").strip()
            partner.is_active = request.POST.get("is_active") in ("on", "1", "true")
            partner.save(update_fields=[
                "name", "partner_type", "contact_name", "contact_email",
                "website", "notes", "is_active", "updated_at",
            ])
            messages.success(request, f"Партнёр «{partner.name}» обновлён.")
            log_staff_action(
                request, AuditAction.PARTNER_UPDATED,
                target=partner.name,
                details={
                    "partner_id": str(partner.id),
                    "is_active_before": before_active, "is_active_after": partner.is_active,
                },
            )
            return redirect("dashboard:partner_detail", partner_id=partner.id)

    context = {
        "page_title": f"{partner.name} — DOPX Staff",
        "active_tab": "ads",
        "partner": partner,
        "partner_type_choices": PartnerType.choices,
        "banners": partner.banners.all().order_by("-priority", "-created_at"),
    }
    return render(request, "dashboard/partner_detail.html", context)


@staff_member_required
@require_POST
def partner_delete(request, partner_id):
    from partners.models import Partner

    partner = get_object_or_404(Partner, id=partner_id)
    name = partner.name
    banner_count = partner.banners.count()
    partner.delete()
    messages.success(
        request,
        f"Партнёр «{name}» удалён" + (f" ({banner_count} баннеров остались без привязки к партнёру)" if banner_count else "") + ".",
    )
    log_staff_action(
        request, AuditAction.PARTNER_DELETED,
        target=name, details={"partner_id": str(partner_id), "banners_orphaned": banner_count},
    )
    return redirect("dashboard:partners_list")


BANNERS_PAGE_SIZE = 30


@staff_member_required
def banners_list(request):
    from partners.models import Banner, BannerZone, Partner

    zone_filter = request.GET.get("zone", "").strip()
    qs = services.banners_queryset(zone=zone_filter)
    banners_page = Paginator(qs, BANNERS_PAGE_SIZE).get_page(request.GET.get("page"))
    context = {
        "page_title": "Баннеры — DOPX Staff",
        "active_tab": "ads",
        "banners": banners_page,
        "zone_filter": zone_filter,
        "zone_choices": BannerZone.choices,
        "partners_for_select": Partner.objects.filter(is_active=True).order_by("name"),
    }
    return render(request, "dashboard/banners_list.html", context)


def _banner_form_fields(request) -> dict:
    """Разбор POST-полей формы баннера (create и update)."""
    starts_at_raw = request.POST.get("starts_at", "").strip()
    ends_at_raw = request.POST.get("ends_at", "").strip()
    starts_at = parse_datetime(starts_at_raw) if starts_at_raw else None
    ends_at = parse_datetime(ends_at_raw) if ends_at_raw else None
    if starts_at and timezone.is_naive(starts_at):
        starts_at = timezone.make_aware(starts_at)
    if ends_at and timezone.is_naive(ends_at):
        ends_at = timezone.make_aware(ends_at)
    partner_id = request.POST.get("partner_id", "").strip()
    try:
        priority = int(request.POST.get("priority", "0") or "0")
    except ValueError:
        priority = 0
    return {
        "zone": request.POST.get("zone", "").strip(),
        "title": request.POST.get("title", "").strip(),
        "target_url": request.POST.get("target_url", "").strip(),
        "partner_id": partner_id or None,
        "is_active": request.POST.get("is_active") in ("on", "1", "true"),
        "requires_age_disclaimer": request.POST.get("requires_age_disclaimer") in ("on", "1", "true"),
        "starts_at": starts_at,
        "ends_at": ends_at,
        "priority": priority,
    }


@staff_member_required
@require_POST
def banner_create(request):
    from partners.models import Banner, BannerZone

    fields = _banner_form_fields(request)
    image = request.FILES.get("image")

    if not fields["title"] or fields["zone"] not in dict(BannerZone.choices) or not fields["target_url"] or not image:
        messages.error(request, "Название, зона, ссылка перехода и изображение обязательны.")
        return redirect("dashboard:banners_list")

    banner = Banner.objects.create(image=image, **fields)
    messages.success(request, f"Баннер «{banner.title}» создан.")
    log_staff_action(
        request, AuditAction.BANNER_CREATED,
        target=banner.title, details={"banner_id": str(banner.id), "zone": banner.zone},
    )
    return redirect("dashboard:banner_detail", banner_id=banner.id)


@staff_member_required
def banner_detail(request, banner_id):
    from partners.models import Banner, BannerZone, Partner

    banner = get_object_or_404(Banner.objects.select_related("partner"), id=banner_id)

    if request.method == "POST":
        fields = _banner_form_fields(request)
        if not fields["title"] or fields["zone"] not in dict(BannerZone.choices) or not fields["target_url"]:
            messages.error(request, "Название, зона и ссылка перехода обязательны.")
        else:
            for key, value in fields.items():
                setattr(banner, key, value)
            update_fields = list(fields.keys()) + ["updated_at"]
            image = request.FILES.get("image")
            if image:
                banner.image = image
                update_fields.append("image")
            banner.save(update_fields=update_fields)
            messages.success(request, f"Баннер «{banner.title}» обновлён.")
            log_staff_action(
                request, AuditAction.BANNER_UPDATED,
                target=banner.title, details={"banner_id": str(banner.id), "image_replaced": bool(image)},
            )
            return redirect("dashboard:banner_detail", banner_id=banner.id)

    context = {
        "page_title": f"{banner.title} — DOPX Staff",
        "active_tab": "banners",
        "banner": banner,
        "zone_choices": BannerZone.choices,
        "partners_for_select": Partner.objects.filter(is_active=True).order_by("name"),
    }
    return render(request, "dashboard/banner_detail.html", context)


@staff_member_required
@require_POST
def banner_delete(request, banner_id):
    from partners.models import Banner

    banner = get_object_or_404(Banner, id=banner_id)
    title = banner.title
    banner.delete()
    messages.success(request, f"Баннер «{title}» удалён.")
    log_staff_action(
        request, AuditAction.BANNER_DELETED,
        target=title, details={"banner_id": str(banner_id)},
    )
    return redirect("dashboard:banners_list")


# ============================================================
# Роли доступа staff по разделам. Проверяем is_superuser напрямую:
# раздел выдачи прав не управляется той же системой прав.
# ============================================================

@staff_member_required
def access_roles_list(request):
    if not request.user.is_superuser:
        from django.core.exceptions import PermissionDenied
        raise PermissionDenied("Управление правами доступа — только для суперпользователей.")

    from .models import DASHBOARD_SECTIONS, StaffAccessGrant

    User = get_user_model()
    staff_users = (
        User.objects.filter(is_staff=True, is_superuser=False)
        .select_related("dashboard_access_grant")
        .order_by("username")
    )
    context = {
        "page_title": "Роли доступа — DOPX Staff",
        "active_tab": "access_roles",
        "staff_users": staff_users,
        "section_count": len(DASHBOARD_SECTIONS),
    }
    return render(request, "dashboard/access_roles_list.html", context)


@staff_member_required
def access_roles_detail(request, user_id):
    if not request.user.is_superuser:
        from django.core.exceptions import PermissionDenied
        raise PermissionDenied("Управление правами доступа — только для суперпользователей.")

    from .models import DASHBOARD_SECTIONS, StaffAccessGrant

    User = get_user_model()
    target_user = get_object_or_404(User, id=user_id, is_staff=True)
    grant = StaffAccessGrant.objects.filter(user=target_user).first()

    if request.method == "POST":
        if request.POST.get("action") == "full_access":
            # Удаляем запись — пользователь снова получает полный доступ.
            if grant:
                grant.delete()
            messages.success(request, f"«{target_user.username}»: ограничения сняты, полный доступ ко всем разделам.")
            log_staff_action(
                request, AuditAction.ACCESS_GRANT_UPDATED,
                target=target_user.username,
                details={"user_id": str(target_user.id), "mode": "full_access_restored"},
            )
        else:
            selected = [key for key, _label in DASHBOARD_SECTIONS if request.POST.get(f"section_{key}") in ("on", "1", "true")]
            if grant:
                grant.allowed_sections = selected
                grant.updated_by = request.user
                grant.save(update_fields=["allowed_sections", "updated_by", "updated_at"])
            else:
                grant = StaffAccessGrant.objects.create(
                    user=target_user, allowed_sections=selected, updated_by=request.user,
                )
            messages.success(request, f"«{target_user.username}»: сохранено {len(selected)} из {len(DASHBOARD_SECTIONS)} разделов.")
            log_staff_action(
                request, AuditAction.ACCESS_GRANT_UPDATED,
                target=target_user.username,
                details={"user_id": str(target_user.id), "allowed_sections": selected},
            )
        return redirect("dashboard:access_roles_detail", user_id=target_user.id)

    allowed = set(grant.allowed_sections) if grant else None  # None — полный доступ
    context = {
        "page_title": f"Доступ: {target_user.username} — DOPX Staff",
        "active_tab": "access_roles",
        "target_user": target_user,
        "grant": grant,
        "sections": [
            {"key": key, "label": label, "checked": allowed is None or key in allowed}
            for key, label in DASHBOARD_SECTIONS
        ],
        "has_restrictions": allowed is not None,
    }
    return render(request, "dashboard/access_roles_detail.html", context)
