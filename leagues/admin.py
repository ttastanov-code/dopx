from django.contrib import admin
from .models import League


@admin.register(League)
class LeagueAdmin(admin.ModelAdmin):

    list_display = (
        "name",
        "country",
        "created_at",
    )

    search_fields = (
        "name",
        "country",
    )