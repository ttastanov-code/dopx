from django.contrib import admin
from .models import Player


@admin.register(Player)
class PlayerAdmin(admin.ModelAdmin):

    list_display = (
        "first_name",
        "last_name",
        "team",
        "position",
        "number",
        "is_active",
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