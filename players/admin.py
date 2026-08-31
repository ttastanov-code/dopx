from django.contrib import admin
from unfold.admin import ModelAdmin

from core.admin_actions import export_as_csv

from .models import Player


# НОВОЕ (2026-08-31): "ушедшие игроки" — parsers/kff/photo_scraper.py
# автоматически снимает is_active игроку, которого N прогонов подряд не
# находит в актуальном составе на kffleague.kz (см. docstring
# match_and_fetch_players_for_team). Это эвристика, а не стопроцентный
# факт — если staff видит ложное срабатывание (например KFF сам не
# успел обновить страницу, или игрок долго восстанавливался после травмы
# и его временно убрали со страницы состава), это действие возвращает
# игрока в активный состав и обнуляет счётчик отсутствия, чтобы отсчёт
# начался заново со следующего прогона.
@admin.action(description="↩️ Вернуть в активный состав (сбросить счётчик отсутствия)")
def reactivate_player(modeladmin, request, queryset):
    updated = queryset.update(is_active=True, roster_absence_streak=0)
    modeladmin.message_user(request, f"Возвращено в активный состав: {updated}.")


@admin.register(Player)
class PlayerAdmin(ModelAdmin):

    list_display = (
        "first_name",
        "last_name",
        "team",
        "position",
        "number",
        "is_active",
        "roster_absence_streak",
    )

    search_fields = (
        "first_name",
        "last_name",
    )

    list_filter = (
        "team",
        "position",
        "is_active",
    )

    autocomplete_fields = ("team",)

    actions = [export_as_csv, reactivate_player]