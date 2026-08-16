# users/admin.py
"""
rating_power не в list_display — поле есть в схеме, но нигде не читается/не
пишется кроме дефолта 1.0, показывать его как метрику вводит в заблуждение
(колонку не дропаем — отдельное решение). registration_ip в list_display/
search — для разбора жалоб на подозрительные регистрации.
"""
from __future__ import annotations

from django.contrib import admin
from unfold.admin import ModelAdmin
from django.utils import timezone

from core.admin_actions import export_as_csv

from .models import Follow, PushSubscription, SuspiciousActivityFlag, User, UserBadge, UserXP


@admin.register(User)
class UserAdmin(ModelAdmin):
    list_display = (
        "username",
        "email",
        "city",
        "trust_score",
        "is_verified",
        "registration_ip",
    )
    search_fields = ("username", "email", "registration_ip")
    list_filter = ("is_verified",)
    actions = [export_as_csv, "verify_selected", "deactivate_selected"]

    @admin.action(description="Отметить как верифицированных")
    def verify_selected(self, request, queryset):
        updated = queryset.update(is_verified=True)
        self.message_user(request, f"Верифицировано: {updated}")

    @admin.action(description="Деактивировать (is_active=False)")
    def deactivate_selected(self, request, queryset):
        # НЕ используем queryset.delete() — деактивация всегда обратима,
        # массовое удаление аккаунтов из списка admin слишком опасная
        # операция, чтобы предлагать её одной кнопкой без подтверждения.
        updated = queryset.update(is_active=False)
        self.message_user(request, f"Деактивировано: {updated}")


@admin.register(UserBadge)
class UserBadgeAdmin(ModelAdmin):
    list_display = ("user", "badge_type", "rarity", "is_secret", "awarded_at")
    list_filter = ("badge_type",)
    search_fields = ("user__username", "user__email")
    autocomplete_fields = ("user",)
    actions = [export_as_csv]


@admin.register(UserXP)
class UserXPAdmin(ModelAdmin):
    list_display = ("user", "level", "total_xp", "progress_percent")
    search_fields = ("user__username", "user__email")
    autocomplete_fields = ("user",)
    actions = [export_as_csv]


@admin.register(SuspiciousActivityFlag)
class SuspiciousActivityFlagAdmin(ModelAdmin):
    """Очередь ручной модерации анти-фрод сигналов."""

    list_display = ("user", "source", "score", "status", "match", "created_at")
    list_filter = ("source", "status")
    search_fields = ("user__username", "user__email")
    autocomplete_fields = ("user", "match", "reviewed_by")
    readonly_fields = ("user", "match", "source", "score", "details", "created_at")
    actions = ["mark_confirmed", "mark_dismissed", export_as_csv]

    @admin.action(description="Отметить как подтверждённую накрутку")
    def mark_confirmed(self, request, queryset):
        updated = queryset.update(status="confirmed", reviewed_by=request.user, reviewed_at=timezone.now())
        self.message_user(request, f"Подтверждено: {updated}")

    @admin.action(description="Отметить как ложное срабатывание")
    def mark_dismissed(self, request, queryset):
        updated = queryset.update(status="dismissed", reviewed_by=request.user, reviewed_at=timezone.now())
        self.message_user(request, f"Отклонено: {updated}")


@admin.register(Follow)
class FollowAdmin(ModelAdmin):
    """Follow-граф — кто на кого подписан."""

    list_display = ("user", "player", "team", "created_at")
    list_filter = ("created_at",)
    search_fields = ("user__username", "user__email", "player__first_name", "player__last_name", "team__name")
    autocomplete_fields = ("user", "player", "team")
    actions = [export_as_csv]


@admin.register(PushSubscription)
class PushSubscriptionAdmin(ModelAdmin):
    """Web Push подписки."""

    list_display = ("user", "user_agent", "created_at")
    search_fields = ("user__username", "user__email", "endpoint")
    autocomplete_fields = ("user",)
    readonly_fields = ("endpoint", "p256dh", "auth")
    actions = [export_as_csv]