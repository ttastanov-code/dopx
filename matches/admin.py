from django.contrib import admin
from unfold.admin import ModelAdmin, TabularInline

from core.admin_actions import export_as_csv
from events.models import MatchEvent
from lineups.models import MatchLineup

from .models import Match, MatchPlayerStatistics, MatchTeamStatistics


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


class MatchTeamStatisticsInline(TabularInline):
    """Объективная статистика команд (KFF) прямо на странице матча —
    для сверки "что видит staff" при разборе флагов stats_divergence
    (dashboard/antifraud), не пересчитывается тут, только read-friendly."""
    model = MatchTeamStatistics
    extra = 0
    fields = ("team", "possession_percent", "shots", "shots_on_goal", "corners", "fouls", "yellow_cards", "red_cards")
    show_change_link = True


@admin.register(Match)
class MatchAdmin(ModelAdmin):

    list_display = (
        "home_team",
        "away_team",
        "start_time",
        "status",
        "manual_override",
        "home_score",
        "away_score",
    )

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

    inlines = [MatchLineupInline, MatchEventInline, MatchTeamStatisticsInline]

    actions = [export_as_csv, "resync_selected", "mark_postponed_manually", "clear_manual_override"]

    @admin.action(description="⏸️ Пометить перенесённым вручную (снять с автосинка)")
    def mark_postponed_manually(self, request, queryset):
        """Для случаев вроде обнаруженного 2026-08-21: KFF показывает на
        своей странице матча баннер "перенесён на неопределённый срок"
        ЗАДОЛГО до того, как реально меняет структурные status/date в API —
        update_match_statuses видит api_status="scheduled" ещё много дней
        и молча откатывал бы ручную правку статуса обратно. Это действие
        ставит status='postponed' И manual_override=True разом — второе
        обязательно, иначе первое переживёт максимум один цикл автосинка
        (10-15 минут). Снимается действием ниже, когда KFF наконец
        опубликует настоящую новую дату."""
        updated = queryset.update(status="postponed", manual_override=True)
        self.message_user(
            request,
            f"Помечено «перенесён» вручную: {updated}. Автосинк не будет трогать статус/дату, "
            f"пока не снимете пометку («Снять ручную пометку»)."
        )

    @admin.action(description="▶️ Снять ручную пометку — вернуть под автосинк")
    def clear_manual_override(self, request, queryset):
        """Снимает manual_override — используйте, когда KFF наконец
        опубликовал реальную новую дату/статус (проверьте на kff.kz), и
        матч можно снова доверить автосинку."""
        updated = queryset.update(manual_override=False)
        self.message_user(request, f"Ручная пометка снята: {updated}. Матч(и) снова под автосинком.")

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


@admin.register(MatchTeamStatistics)
class MatchTeamStatisticsAdmin(ModelAdmin):
    """Отдельный список (не только инлайн на матче) — для точечного поиска
    "какая команда/матч уже досинхронизированы объективной статистикой",
    используется при разборе флагов stats_divergence."""

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
