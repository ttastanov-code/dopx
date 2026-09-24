# core/admin_mixins.py
"""Общие миксины для ModelAdmin."""
from __future__ import annotations


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
