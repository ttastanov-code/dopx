from django.contrib import admin
from django.utils import timezone
from unfold.admin import ModelAdmin

from core.admin_actions import export_as_csv
from core.admin_mixins import SuperuserOnlyAdminMixin

from .models import Player, PotentialDuplicatePlayer


# Вернуть игрока в активный состав и обнулить счётчик отсутствия.
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


# Флаги возможных дублей — отметить разобранными; слияние — в дашборде.
@admin.action(description="✅ Отметить разобранным")
def mark_reviewed(modeladmin, request, queryset):
    updated = queryset.filter(reviewed=False).update(
        reviewed=True, reviewed_by=request.user, reviewed_at=timezone.now(),
    )
    modeladmin.message_user(request, f"Отмечено разобранным: {updated}.")


@admin.register(PotentialDuplicatePlayer)
class PotentialDuplicatePlayerAdmin(SuperuserOnlyAdminMixin, ModelAdmin):

    list_display = (
        "existing_player",
        "new_player",
        "team_label",
        "reviewed",
        "reviewed_by",
        "created_at",
    )

    list_filter = ("reviewed", "team_label")

    search_fields = (
        "existing_player__first_name", "existing_player__last_name",
        "new_player__first_name", "new_player__last_name",
        "team_label",
    )

    autocomplete_fields = ("existing_player", "new_player")

    readonly_fields = ("created_at",)

    actions = [mark_reviewed]