from django.contrib import admin
from unfold.admin import ModelAdmin

from core.admin_actions import export_as_csv

from .models import League


@admin.register(League)
class LeagueAdmin(ModelAdmin):

    list_display = (
        "name",
        "country",
        "created_at",
    )

    search_fields = (
        "name",
        "country",
    )

    actions = [export_as_csv]