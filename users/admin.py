from django.contrib import admin
from .models import User


@admin.register(User)
class UserAdmin(admin.ModelAdmin):

    list_display = (
        "username",
        "email",
        "city",
        "trust_score",
        "rating_power",
        "is_verified",
    )

    search_fields = (
        "username",
        "email",
    )