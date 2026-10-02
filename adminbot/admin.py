from django.contrib import admin
from unfold.admin import ModelAdmin

from core.admin_mixins import SuperuserOnlyAdminMixin

from .models import BotLink, ChannelPost, Incident


@admin.register(BotLink)
class BotLinkAdmin(SuperuserOnlyAdminMixin, ModelAdmin):
    """Привязка Telegram к аккаунту = вход в бот от его имени: только суперпользователю."""

    list_display = ("user", "telegram_id", "telegram_name", "notify", "linked_at", "last_seen")
    search_fields = ("user__username", "telegram_name")
    readonly_fields = ("linked_at", "last_seen")


@admin.register(ChannelPost)
class ChannelPostAdmin(SuperuserOnlyAdminMixin, ModelAdmin):
    """Работа с постами — в дашборде «Telegram-канал»; здесь — для разбора."""

    list_display = ("kind", "status", "scheduled_at", "published_at", "created_at")
    list_filter = ("kind", "status")
    search_fields = ("text", "key")
    readonly_fields = ("message_id", "published_at", "published_by", "created_by", "created_at", "updated_at")


@admin.register(Incident)
class IncidentAdmin(SuperuserOnlyAdminMixin, ModelAdmin):
    list_display = ("title", "key", "level", "acked_by", "acked_at", "resolved_at", "created_at")
    list_filter = ("resolved_at",)
    search_fields = ("title", "key")
    readonly_fields = [f.name for f in Incident._meta.fields]
