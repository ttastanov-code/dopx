# users/admin.py
"""Админка users. rating_power не показываем — поле не используется."""
from __future__ import annotations

from django.contrib import admin
from unfold.admin import ModelAdmin
from django.utils import timezone

from core.admin_actions import export_as_csv

from .models import AntiFraudThreshold, Follow, PushSubscription, SuspiciousActivityFlag, User, UserBadge, UserXP


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
    # trust_score только для чтения — считается автоматически.
    readonly_fields = ("trust_score",)
    # Хэш пароля не редактируем: смена — через сброс пароля.
    exclude = ("password",)
    actions = [export_as_csv, "verify_selected", "deactivate_selected"]

    # Права и статус меняет только суперпользователь (иначе можно выдать себе superuser).
    PRIVILEGE_FIELDS = ("is_superuser", "is_staff", "groups", "user_permissions")

    def get_readonly_fields(self, request, obj=None):
        fields = super().get_readonly_fields(request, obj)
        if request.user.is_superuser:
            return fields
        return (*fields, *self.PRIVILEGE_FIELDS)

    @admin.action(description="Отметить как верифицированных")
    def verify_selected(self, request, queryset):
        updated = queryset.update(is_verified=True)
        self.message_user(request, f"Верифицировано: {updated}")

    @admin.action(description="Деактивировать (is_active=False)")
    def deactivate_selected(self, request, queryset):
        # Только деактивация, без массового удаления.
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
    # total_xp/level только для чтения — начисляются событиями.
    readonly_fields = ("total_xp", "level")
    actions = [export_as_csv]


@admin.register(SuspiciousActivityFlag)
class SuspiciousActivityFlagAdmin(ModelAdmin):
    """Очередь модерации антифрод-флагов."""

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
        """Отклонить флаги. Для stats_divergence / player_stats_divergence
        дополнительно снимается поправка рейтинга и ставится cooldown.
        """
        # Общая логика с дашбордом — aggregates.tasks.apply_divergence_dismissal.
        from aggregates.tasks import apply_divergence_dismissal

        apply_divergence_dismissal(list(queryset))

        updated = queryset.update(status="dismissed", reviewed_by=request.user, reviewed_at=timezone.now())
        self.message_user(request, f"Отклонено: {updated}")


@admin.register(AntiFraudThreshold)
class AntiFraudThresholdAdmin(ModelAdmin):
    """Самокалибрующиеся антифрод-пороги. Значения можно править вручную;
    выход за min/max исправит следующая калибровка.
    """

    list_display = ("key", "value", "default_value", "min_value", "max_value", "last_note", "updated_at")
    list_editable = ("value",)
    readonly_fields = ("key", "default_value", "created_at", "updated_at")
    search_fields = ("key",)
    actions = [export_as_csv]


@admin.register(Follow)
class FollowAdmin(ModelAdmin):
    """Подписки."""

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