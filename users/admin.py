# users/admin.py
"""
ИЗМЕНЕНИЯ (продуктовый аудит DOPX, часть 2):

1. `rating_power` убран из `UserAdmin.list_display`. Поле существует в
   схеме (см. `users/models.py`), но нигде в проекте не читается и не
   пишется, кроме дефолтного значения `1.0` при создании — показывать его
   в списке пользователей вводит в заблуждение (выглядит как значимая
   метрика, а на деле всегда `1.0` у всех). Саму колонку НЕ удаляю здесь —
   это отдельное, необратимое решение (drop column = потеря данных, если в
   проде там что-то реально накопилось за время эксплуатации), должно быть
   осознанным отдельным шагом, а не побочным эффектом продуктового аудита.
   Добавлен `registration_ip` в list_display/search — пригодится при
   разборе жалоб на подозрительные регистрации (см. `users/views.py::
   RegisterView`).
2. Добавлена регистрация `UserBadge` (было — не в admin.py вообще, только
   доступ через ORM/шаблон профиля) и НОВАЯ `SuspiciousActivityFlagAdmin` —
   очередь ручной модерации анти-фрод сигналов (см. `users/models.py::
   SuspiciousActivityFlag`). Раньше в проекте не было ВООБЩЕ никакого
   admin-интерфейса для разбора подозрительной активности.
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
    """Follow-граф (продуктовый аудит, раздел 5b) — кто на кого подписан."""

    list_display = ("user", "player", "team", "created_at")
    list_filter = ("created_at",)
    search_fields = ("user__username", "user__email", "player__first_name", "player__last_name", "team__name")
    autocomplete_fields = ("user", "player", "team")
    actions = [export_as_csv]


@admin.register(PushSubscription)
class PushSubscriptionAdmin(ModelAdmin):
    """Web Push подписки (продуктовый аудит, раздел 5c)."""

    list_display = ("user", "user_agent", "created_at")
    search_fields = ("user__username", "user__email", "endpoint")
    autocomplete_fields = ("user",)
    readonly_fields = ("endpoint", "p256dh", "auth")
    actions = [export_as_csv]