# engagement/admin.py — Админка вовлечения: мнения экспертов, лиги, приглашения.
from django.contrib import admin
from unfold.admin import ModelAdmin

from .models import DailyPoll, DailyStreak, ExpertTake, FriendLeague, Referral, SeasonPass


@admin.register(ExpertTake)
class ExpertTakeAdmin(ModelAdmin):
    list_display = ("match", "author_title", "is_published", "created_at")
    list_filter = ("is_published",)
    raw_id_fields = ("match", "key_player")
    exclude = ("author",)

    def save_model(self, request, obj, form, change):
        if not obj.author_id:
            obj.author = request.user
        super().save_model(request, obj, form, change)


@admin.register(FriendLeague)
class FriendLeagueAdmin(ModelAdmin):
    list_display = ("name", "owner", "season", "invite_code", "created_at")
    search_fields = ("name", "invite_code", "owner__username")
    raw_id_fields = ("owner", "season")


@admin.register(Referral)
class ReferralAdmin(ModelAdmin):
    list_display = ("inviter", "invited", "source", "rewarded_at", "created_at")
    list_filter = ("source",)
    raw_id_fields = ("inviter", "invited")


@admin.register(DailyStreak)
class DailyStreakAdmin(ModelAdmin):
    list_display = ("user", "current", "best", "freezes", "last_active_date")
    raw_id_fields = ("user",)


@admin.register(SeasonPass)
class SeasonPassAdmin(ModelAdmin):
    list_display = ("user", "season", "xp")
    raw_id_fields = ("user", "season")


@admin.register(DailyPoll)
class DailyPollAdmin(ModelAdmin):
    list_display = ("question", "kind", "match", "score", "closes_at", "created_at")
    list_filter = ("kind",)
    raw_id_fields = ("match", "event", "player_a", "player_b")
