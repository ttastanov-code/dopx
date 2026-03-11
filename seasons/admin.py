from django.contrib import admin
from .models import Season


@admin.register(Season)
class SeasonAdmin(admin.ModelAdmin):

    list_display = (
        "league",
        "year",
        "is_active",
    )

    list_filter = (
        "league",
        "is_active",
    )