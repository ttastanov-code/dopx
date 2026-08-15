from django.contrib import admin
from unfold.admin import ModelAdmin, TabularInline

from core.admin_actions import export_as_csv
from events.models import MatchEvent
from lineups.models import MatchLineup

from .models import Match


class MatchEventInline(TabularInline):
    """События матча прямо на странице матча — раньше редактировались
    только отдельным списком в events/admin.py, приходилось помнить
    UUID матча и фильтровать. extra=0 — не плодить пустые черновые строки
    на каждый заход (у "живого" матча уже могут быть десятки событий)."""
    model = MatchEvent
    extra = 0
    fields = ("minute", "event_type", "team_side", "player", "assist_player")
    autocomplete_fields = ("player", "assist_player")
    show_change_link = True


class MatchLineupInline(TabularInline):
    model = MatchLineup
    extra = 0
    fields = ("team", "side", "formation")
    show_change_link = True


@admin.register(Match)
class MatchAdmin(ModelAdmin):

    list_display = (
        "home_team",
        "away_team",
        "start_time",
        "status",
        "home_score",
        "away_score",
    )

    list_filter = (
        "league",
        "season",
        "status",
    )

    search_fields = (
        "home_team__name",
        "away_team__name",
        "external_id",
    )

    # Match — самая "central hub" модель проекта (7 FK) — до этого ни один
    # из них не был autocomplete, значит редактирование матча грузило ПОЛНЫЙ
    # <select> со всеми командами/тренерами/судьями/стадионами в БД разом.
    # Все 8 целевых моделей уже имеют search_fields (проверено/дополнено
    # в leagues/seasons/teams/coaches/referees/core admin.py) — обязательное
    # условие для autocomplete_fields, иначе Django падает системной проверкой.
    autocomplete_fields = (
        "league", "season", "home_team", "away_team",
        "home_coach", "away_coach", "referee", "stadium",
    )

    inlines = [MatchLineupInline, MatchEventInline]

    actions = [export_as_csv, "resync_selected"]

    @admin.action(description="Пересинхронизировать выбранные матчи с KFF")
    def resync_selected(self, request, queryset):
        """Массовый ресинк — та же логика, что кнопка «Досинхронизировать»
        на /staff/dashboard/data-health/ (dashboard/parser_tools.py::resync_match),
        просто применённая сразу к нескольким матчам из списка admin.
        Синхронно, один HTTP-запрос staff = ожидание N матчей — ок для
        точечной работы с десятком строк, для массового полного синка
        сезона используется celery-задача sync_kff_premier_league."""
        from dashboard.audit import log_staff_action
        from dashboard.models import AuditAction
        from dashboard.parser_tools import resync_match

        ok_count, fail_count = 0, 0
        for match in queryset:
            success, _message = resync_match(match)
            ok_count += int(success)
            fail_count += int(not success)

        log_staff_action(
            request, AuditAction.MATCH_RESYNC,
            target=f"bulk: {queryset.count()} матчей",
            details={"ok": ok_count, "failed": fail_count, "via": "admin_bulk_action"},
        )
        self.message_user(request, f"Ресинк завершён: успешно {ok_count}, с ошибкой {fail_count}")
