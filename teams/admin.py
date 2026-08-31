from django.contrib import admin
from unfold.admin import ModelAdmin

from core.admin_actions import export_as_csv

from .models import Team, TeamSeason
from .services import extract_team_colors


# НОВОЕ (2026-08-31): ручной пересчёт Team.primary_color/secondary_color
# для выбранных команд — на случай, если логотип поменяли и нужно
# обновить палитру hero-баннера без ожидания следующего запуска
# management-команды compute_team_colors. См.
# teams/services.py::extract_team_colors.
@admin.action(description="🎨 Пересчитать цвета бренда (из логотипа)")
def recompute_primary_color(modeladmin, request, queryset):
    updated = 0
    skipped = 0
    for team in queryset:
        primary, secondary = extract_team_colors(team)
        if primary:
            team.primary_color = primary
            team.secondary_color = secondary or ""
            team.save(update_fields=["primary_color", "secondary_color"])
            updated += 1
        else:
            skipped += 1
    modeladmin.message_user(
        request,
        f"Цвета посчитаны для {updated} команд(ы). Пропущено (нет логотипа/не читается): {skipped}.",
    )


@admin.register(Team)
class TeamAdmin(ModelAdmin):

    list_display = (
        "name",
        "city",
        "external_id",
        "primary_color",
        "secondary_color",
    )

    search_fields = (
        "name",
        "city",
    )

    # НОВОЕ: один раз проставить пары дерби-соперников для бейджа
    # "derby_hunter" (users/badges.py) — удобный виджет "выбрать несколько
    # из списка" вместо голого multiple-select.
    filter_horizontal = ("rivals",)

    actions = [export_as_csv, recompute_primary_color]


@admin.register(TeamSeason)
class TeamSeasonAdmin(ModelAdmin):

    list_display = (
        "team",
        "season",
    )

    list_filter = (
        "season",
    )

    search_fields = (
        "team__name",
    )

    autocomplete_fields = ("team", "season")

    actions = [export_as_csv]