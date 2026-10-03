from django.contrib import admin
from unfold.admin import ModelAdmin

from .models import TelegramAccount


@admin.register(TelegramAccount)
class TelegramAccountAdmin(ModelAdmin):
    list_display = ("user", "telegram_id", "username", "can_message", "notify", "linked_at", "last_seen")
    list_filter = ("can_message", "notify")
    search_fields = ("user__username", "username")
    readonly_fields = ("linked_at", "last_seen")
    autocomplete_fields = ("user",)
