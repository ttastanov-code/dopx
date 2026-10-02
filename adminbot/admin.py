from django.contrib import admin
from unfold.admin import ModelAdmin

from core.admin_mixins import SuperuserOnlyAdminMixin

from .models import BotLink


@admin.register(BotLink)
class BotLinkAdmin(SuperuserOnlyAdminMixin, ModelAdmin):
    """Привязка Telegram к аккаунту = вход в бот от его имени: только суперпользователю."""

    list_display = ("user", "telegram_id", "telegram_name", "notify", "linked_at", "last_seen")
    search_fields = ("user__username", "telegram_name")
    readonly_fields = ("linked_at", "last_seen")
