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
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.admin_actions import _csv_safe
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
def data_health_partial(request):
    """Той же контент, что и data_health(), но без base.html/_nav.html —
    цель HTMX-поллинга (hx-get каждые 15с, см. templates/dashboard/
    data_health.html). Тот же паттерн, что уже используется для
    live-обновления шапки/событий матча (templates/matches/_match_header.html,
    _match_events.html)."""
    context = {
        "health": services.data_health_summary(),
        "infra": infra_services.infra_health(),
    }
    return render(request, "dashboard/_data_health_content.html", context)


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

    # 2026-08-23, anti-brigading: flag.user может быть None у entity-level
    # сигналов (source="vote_spike" — аномалия у игрока/команды/тренера,
    # а не у конкретного пользователя, см. users/models.py::
    # SuspiciousActivityFlag). target — content_object в этом случае.
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
        # flag.user может быть None у entity-level сигналов (vote_spike) —
        # см. коммент в antifraud_flag_action выше.
        who = flag.user.username if flag.user else f"[сущность] {flag.content_object or '—'}"
        # _csv_safe — защита от CSV/formula injection (see core/admin_actions.py):
        # who/subject приходят из пользовательского ввода (username, тема диспута).
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
    search_year_param = request.GET.get("year", "")
    search_year = int(search_year_param) if search_year_param.isdigit() else None
    search = (
        parser_tools.search_matches(search_query, year=search_year) if search_query
        else {"results": [], "total_count": 0, "year": search_year or timezone.now().year}
    )

    context = {
        "page_title": "Парсер — DOPX Staff",
        "active_tab": "parser_tools",
        "raw_endpoints": parser_tools.RAW_ENDPOINTS,
        "raw_form": raw_form,
        "raw_result": raw_result,
        "triggerable_tasks": parser_tools.TRIGGERABLE_TASKS,
        "task_descriptions": parser_tools.TASK_DESCRIPTIONS,
        "search_query": search_query,
        "search_results": search["results"],
        "search_total_count": search["total_count"],
        "search_year": search["year"],
        "search_available_years": parser_tools.available_search_years(),
        "celery_tasks": parser_tools.list_active_celery_tasks(),
    }
    return render(request, "dashboard/parser_tools.html", context)


@staff_member_required
def parser_tasks_partial(request):
    """Карточка «Очередь celery» на странице parser_tools — цель
    HTMX-поллинга (hx-get каждые 10с). Только этот кусок, а не вся
    страница — иначе перетирались бы форма поиска и просмотр сырого
    ответа KFF, которые staff мог только что заполнить (см. комментарий
    в _celery_tasks_card.html)."""
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


# ============================================================
# Реклама и виджеты: единая staff-страница по ВСЕЙ партнёрской монетизации —
# и баннерам (чужая реклама у нас на сайте), и embed-виджетам (наш контент
# на чужом сайте). Раньше это были две несвязанные истории: у виджетов не
# было вообще никакой staff-страницы (код для игрока/команды можно было
# получить только зайдя на его страницу на сайте, у standings-виджета не
# было и этого), а статистика баннеров/партнёрских рефералок была видна
# только построчно в Django admin — нигде не было общей картины "сколько
# у нас показов/кликов/визитов по рекламе за месяц в целом". Эта страница
# закрывает оба пробела сразу: инструкция + живое превью + генератор
# embed-кода для виджетов, и сводные карточки + топ-N для баннеров и
# партнёрских рефералок (partners/selectors.py).
# ============================================================

def _ads_stats_context() -> dict:
    """
    Вся статистика за 30 дней (виджеты + баннеры + рефералки) — вынесена
    из ads() отдельно, чтобы её могли считать И обычный рендер страницы
    (первая отрисовка), И ads_stats_partial() (HTMX-поллинг, тот же
    паттерн, что у data_health_partial()/_data_health_content.html) без
    дублирования логики batch-резолва id → объект.
    """
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

    top_players_raw = top_widget_entities("player", days=30, limit=10)
    top_teams_raw = top_widget_entities("team", days=30, limit=10)

    players_by_id = {
        str(p.id): p for p in Player.objects.filter(id__in=[r["entity_id"] for r in top_players_raw])
    }
    teams_by_id = {
        str(t.id): t for t in Team.objects.filter(id__in=[r["entity_id"] for r in top_teams_raw])
    }

    top_banners_raw = top_banners(days=30, limit=10)
    banners_by_id = {
        str(b.id): b for b in Banner.objects.select_related("partner").filter(id__in=[r["banner_id"] for r in top_banners_raw])
    }

    top_partners_raw = top_partners_by_referral_visits(days=30, limit=10)
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
        "widget_totals": widget_embed_totals(days=30),
        "banner_totals": banner_totals(days=30),
        "top_banners": [
            {"banner": banners_by_id[r["banner_id"]], "impressions": r["impressions"], "clicks": r["clicks"], "ctr_percent": r["ctr_percent"]}
            for r in top_banners_raw if r["banner_id"] in banners_by_id
        ],
        "referral_visits_total": partner_referral_totals(days=30),
        "top_partners": [
            {"partner": partners_by_slug[r["partner_slug"]], "visits": r["visits"]}
            for r in top_partners_raw if r["partner_slug"] in partners_by_slug
        ],
    }


@staff_member_required
def ads(request):
    """
    /staff/dashboard/ads/ — центральная страница по рекламе и виджетам.
    q_player/q_team — независимые поля поиска (не один общий q, т.к. это
    два разных типа сущностей с разным embed-кодом); выбранный результат
    кладём в контекст, чтобы staff сразу видел готовый код и живое превью,
    не уходя на сайт искать нужного игрока/команду вручную.
    """
    from core.utils import normalize_kz
    from players.models import Player
    from teams.models import Team

    q_player = request.GET.get("q_player", "").strip()
    q_team = request.GET.get("q_team", "").strip()
    player_id = request.GET.get("player_id", "")
    team_id = request.GET.get("team_id", "")

    # normalize_kz — тот же паттерн, что уже используется в поиске команд/
    # игроков/тренеров/судей на сайте и в парсер-тулинге staff-дашборда
    # (core/utils.py): "Актобе" находит "Ақтөбе" независимо от того, какой
    # раскладкой набирали название/фамилию. Раньше здесь был обычный
    # icontains без нормализации — казахские названия по-русски не находились.
    if q_player:
        normalized_q = normalize_kz(q_player)
        player_results = [
            p for p in Player.objects.select_related("team").only("id", "first_name", "last_name", "team")
            if normalized_q in normalize_kz(f"{p.first_name} {p.last_name}")
        ][:10]
    else:
        player_results = []

    if q_team:
        normalized_q = normalize_kz(q_team)
        team_results = [
            t for t in Team.objects.only("id", "name")
            if normalized_q in normalize_kz(t.name)
        ][:10]
    else:
        team_results = []

    # Превью по умолчанию (страница без поиска) — берём произвольного
    # игрока/команду с данными, чтобы виджет не пустовал при первом заходе.
    # player_id/team_id — явный выбор ОДНОГО конкретного результата из
    # списка совпадений поиска (клик по бейджу в шаблоне), без него по
    # умолчанию берётся первый найденный.
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

    # Четвёртый виджет (продуктовый запрос 2026-08-22 — "дать возможность
    # вставлять сборную DOPX на другие сайты"): season_id не передаём —
    # widget всегда берёт активный сезон главной лиги (Season.get_primary_active),
    # тот же принцип "без выбора", что и у standings_embed выше.
    best_xi_url = request.build_absolute_uri(reverse("season_squad:widget"))
    best_xi_embed = _embed_code(best_xi_url, "Сборная DOPX сезона на DOPX", width=320, height=420)

    # Пятый виджет (продуктовый запрос 2026-08-22): "DOPX Лучшие тура" —
    # season_id/tour не передаём, тот же принцип "без выбора" (активный
    # сезон главной лиги + последний завершённый тур), что у best_xi_embed.
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
    """Тот же контент, что и статистический блок ads(), без base.html/
    _nav.html — цель HTMX-поллинга (hx-get каждые 20с на этом же блоке),
    тот же паттерн, что и dashboard:data_health_partial. Поиск/превью
    виджета НЕ в зоне автообновления — если staff начал набирать имя
    игрока, очередной poll не должен затирать недопечатанное значение."""
    return render(request, "dashboard/_ads_stats_content.html", _ads_stats_context())


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
