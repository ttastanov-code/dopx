from django.contrib import admin
from django.shortcuts import redirect
from unfold.admin import ModelAdmin, TabularInline
from unfold.decorators import action

from core.admin_actions import export_as_csv
from events.models import MatchEvent
from lineups.models import MatchLineup

from .models import Match, MatchPlayerStatistics, MatchReaction, MatchTeamStatistics


class MatchEventInline(TabularInline):
    """События матча инлайном. extra=0."""
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


class MatchTeamStatisticsInline(TabularInline):
    """Статистика команд инлайном — для разбора флагов stats_divergence."""
    model = MatchTeamStatistics
    extra = 0
    fields = ("team", "possession_percent", "shots", "shots_on_goal", "corners", "fouls", "yellow_cards", "red_cards")
    show_change_link = True


@admin.register(Match)
class MatchAdmin(ModelAdmin):
    # Кнопка на карточке записи: та же запись в staff-дашборде (действия, история, проверки).
    actions_detail = ["open_in_dashboard"]

    @action(description="Открыть в дашборде", url_path="open-in-dashboard", icon="space_dashboard")
    def open_in_dashboard(self, request, object_id):
        return redirect("dashboard:match_detail", match_id=object_id)


    list_display = ("match_title", "tour", "start_time", "status", "score", "manual_flag")
    list_display_links = ("match_title",)
    list_select_related = ("home_team", "away_team")

    @admin.display(description="Матч", ordering="home_team__name")
    def match_title(self, obj):
        return f"{obj.home_team} — {obj.away_team}"

    @admin.display(description="Счёт")
    def score(self, obj):
        return f"{obj.home_score}:{obj.away_score}" if obj.status in ("live", "finished") else "—"

    # Отметка только у матчей с ручным режимом: «нет» в каждой строке — шум.
    @admin.display(description="Вручную", ordering="manual_override")
    def manual_flag(self, obj):
        return "✋ вручную" if obj.manual_override else ""

    list_filter = (
        "league",
        "season",
        "status",
        "manual_override",
    )

    search_fields = (
        "home_team__name",
        "away_team__name",
        "external_id",
    )

    # autocomplete_fields — без огромных <select>. У целевых моделей есть search_fields.
    autocomplete_fields = (
        "league", "season", "home_team", "away_team",
        "home_coach", "away_coach", "referee",
    )

    inlines = [MatchLineupInline, MatchEventInline, MatchTeamStatisticsInline]

    actions = [export_as_csv, "resync_selected", "mark_postponed_manually", "clear_manual_override"]

    @admin.action(description="⏸️ Пометить перенесённым вручную (снять с автосинка)")
    def mark_postponed_manually(self, request, queryset):
        """status='postponed' + manual_override=True, чтобы автосинк не откатил."""
        updated = queryset.update(status="postponed", manual_override=True)
        self.message_user(
            request,
            f"Помечено «перенесён» вручную: {updated}. Автосинк не будет трогать статус/дату, "
            f"пока не снимете пометку («Снять ручную пометку»)."
        )

    @admin.action(description="▶️ Снять ручную пометку — вернуть под автосинк")
    def clear_manual_override(self, request, queryset):
        """Снимает manual_override — матч снова под автосинком."""
        updated = queryset.update(manual_override=False)
        self.message_user(request, f"Ручная пометка снята: {updated}. Матч(и) снова под автосинком.")

    @admin.action(description="Пересинхронизировать выбранные матчи")
    def resync_selected(self, request, queryset):
        """Ресинк выбранных матчей (только с sportmonks_id), синхронно."""
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


@admin.register(MatchTeamStatistics)
class MatchTeamStatisticsAdmin(ModelAdmin):
    """Статистика команд отдельным списком."""

    list_display = ("team", "match", "possession_percent", "shots", "shots_on_goal", "corners", "fouls", "yellow_cards")
    list_filter = ("team",)
    search_fields = ("team__name", "match__home_team__name", "match__away_team__name")
    autocomplete_fields = ("match", "team")
    actions = [export_as_csv]


@admin.register(MatchPlayerStatistics)
class MatchPlayerStatisticsAdmin(ModelAdmin):
    list_display = ("player", "team", "match", "shots", "shots_on_target", "fouls", "saves")
    list_filter = ("team",)
    search_fields = ("player__first_name", "player__last_name", "team__name")
    autocomplete_fields = ("match", "player", "team")
    actions = [export_as_csv]


@admin.register(MatchReaction)
class MatchReactionAdmin(ModelAdmin):
    """Реакции на завершённый матч — просмотр/модерация."""
    list_display = ("match", "user", "reaction", "created_at")
    list_filter = ("reaction",)
    search_fields = ("match__home_team__name", "match__away_team__name", "user__username")
    autocomplete_fields = ("match", "user")
