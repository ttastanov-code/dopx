# core/admin_mixins.py
"""Общие миксины для ModelAdmin."""
from __future__ import annotations


# Модели, через которые можно получить чужие права: в /admin/ — только суперпользователю.
SUPERUSER_ONLY_MODELS = (
    "auth.Group", "otp_totp.TOTPDevice", "otp_static.StaticDevice",
    "axes.AccessAttempt", "axes.AccessLog", "axes.AccessFailureLog",
)


def lock_superuser_only_models(site) -> None:
    """Перерегистрирует чужие ModelAdmin (auth, otp, axes) с SuperuserOnlyAdminMixin."""
    from django.apps import apps

    for label in SUPERUSER_ONLY_MODELS:
        try:
            model = apps.get_model(label)
        except LookupError:
            continue
        current = site._registry.get(model)
        if current is None or isinstance(current, SuperuserOnlyAdminMixin):
            continue
        site.unregister(model)
        site.register(model, type(f"Locked{type(current).__name__}", (SuperuserOnlyAdminMixin, type(current)), {}))


class SuperuserOnlyAdminMixin:
    """Модель в /admin/ видна и правится только суперпользователем.
    Для моделей, доступ к которым управляется разделом дашборда: без этого права
    на модель конфликтовали бы с настройкой разделов в «Ролях доступа».
    """

    def has_module_permission(self, request):
        return request.user.is_active and request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_active and request.user.is_superuser

    def has_add_permission(self, request, *args, **kwargs):
        return request.user.is_active and request.user.is_superuser and super().has_add_permission(request, *args, **kwargs)

    def has_change_permission(self, request, obj=None):
        return request.user.is_active and request.user.is_superuser and super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return request.user.is_active and request.user.is_superuser and super().has_delete_permission(request, obj)
