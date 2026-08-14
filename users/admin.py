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
from django.utils import timezone

from .models import SuspiciousActivityFlag, User, UserBadge, UserXP


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
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


@admin.register(UserBadge)
class UserBadgeAdmin(admin.ModelAdmin):
    list_display = ("user", "badge_type", "rarity", "is_secret", "awarded_at")
    list_filter = ("badge_type",)
    search_fields = ("user__username", "user__email")
    autocomplete_fields = ("user",)


@admin.register(UserXP)
class UserXPAdmin(admin.ModelAdmin):
    list_display = ("user", "level", "total_xp", "progress_percent")
    search_fields = ("user__username", "user__email")


@admin.register(SuspiciousActivityFlag)
class SuspiciousActivityFlagAdmin(admin.ModelAdmin):
    """Очередь ручной модерации анти-фрод сигналов."""

    list_display = ("user", "source", "score", "status", "match", "created_at")
    list_filter = ("source", "status")
    search_fields = ("user__username", "user__email")
    autocomplete_fields = ("user", "match", "reviewed_by")
    readonly_fields = ("user", "match", "source", "score", "details", "created_at")
    actions = ["mark_confirmed", "mark_dismissed"]

    @admin.action(description="Отметить как подтверждённую накрутку")
    def mark_confirmed(self, request, queryset):
        updated = queryset.update(status="confirmed", reviewed_by=request.user, reviewed_at=timezone.now())
        self.message_user(request, f"Подтверждено: {updated}")

    @admin.action(description="Отметить как ложное срабатывание")
    def mark_dismissed(self, request, queryset):
        updated = queryset.update(status="dismissed", reviewed_by=request.user, reviewed_at=timezone.now())
        self.message_user(request, f"Отклонено: {updated}")