# engagement/admin.py — Админка вовлечения: мнения экспертов, лиги, приглашения.
from django.contrib import admin
from unfold.admin import ModelAdmin

from .models import DailyPoll, DailyStreak, Expert, ExpertInvite, ExpertTake, FriendLeague, Referral, SeasonPass


@admin.register(Expert)
class ExpertAdmin(ModelAdmin):
    list_display = ("name", "title", "is_active")
    search_fields = ("name",)


@admin.register(ExpertInvite)
class ExpertInviteAdmin(ModelAdmin):
    """Ссылки для экспертов. Создавать удобнее в дашборде: Эксперты → Ссылка для эксперта."""

    list_display = ("__str__", "match", "status_label", "takes_label", "expires_at", "created_at")
    list_filter = ("auto_publish",)
    search_fields = ("expert__name", "note")
    raw_id_fields = ("match", "expert")
    readonly_fields = ("token", "link", "created_by", "last_used_at", "created_at")
    actions = ("revoke",)

    STATUS = {"active": "действует", "used": "всё написано", "expired": "истекла", "revoked": "отключена"}

    @admin.display(description="Статус")
    def status_label(self, obj):
        return self.STATUS[obj.status()]

    @admin.display(description="Мнений")
    def takes_label(self, obj):
        return f"{obj.takes_used()} из {obj.max_takes}"

    @admin.display(description="Ссылка")
    def link(self, obj):
        from django.urls import reverse

        return reverse("engagement:expert_write", args=[obj.token]) if obj.pk else ""

    @admin.action(description="Отключить выбранные ссылки")
    def revoke(self, request, queryset):
        from django.utils import timezone

        updated = queryset.filter(revoked_at__isnull=True).update(revoked_at=timezone.now())
        self.message_user(request, f"Отключено ссылок: {updated}.")


# Удобная форма — в дашборде: Эксперты.
@admin.register(ExpertTake)
class ExpertTakeAdmin(ModelAdmin):
    list_display = ("match", "expert", "headline", "is_published", "created_at")
    list_filter = ("is_published", "expert")
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
