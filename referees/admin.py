from django.contrib import admin
from .models import Referee


@admin.register(Referee)
class RefereeAdmin(admin.ModelAdmin):

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