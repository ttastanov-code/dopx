from django.contrib import admin
from unfold.admin import ModelAdmin

from core.admin_actions import export_as_csv

from .models import Referee


@admin.register(Referee)
class RefereeAdmin(ModelAdmin):

    list_display = (
        "first_name",
        "last_name",
        "country",
        "is_active",
    )

    search_fields = (
        "first_name",
        "last_name",
    )

    actions = [export_as_csv]