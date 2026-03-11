from django.contrib import admin
from .models import Coach


@admin.register(Coach)
class CoachAdmin(admin.ModelAdmin):

    list_display = (
        "first_name",
        "last_name",
        "team",
        "is_active",
    )

    search_fields = (
        "first_name",
        "last_name",
    )

    list_filter = (
        "team",
    )