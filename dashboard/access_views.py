# dashboard/access_views.py
"""«Роли доступа»: группы прав /admin/, членство в группах, выдача и снятие staff.
Всё — только суперпользователю; каждое действие пишется в аудит.
"""
from __future__ import annotations

from functools import wraps

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .admin_access import (
    CRUD_LABELS, groups_with_counts, permission_matrix, set_group_permissions, superuser_only_models,
)
from .audit import log_staff_action
from .models import AuditAction

GROUP_NAME_MAX = 150


def superuser_required(view):
    @staff_member_required
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_superuser:
            raise PermissionDenied("Управление правами — только для суперпользователей.")
        return view(request, *args, **kwargs)
    return wrapper


def _audit(request, target: str, **details) -> None:
    log_staff_action(request, AuditAction.ACCESS_GRANT_UPDATED, target=target, details=details)


@superuser_required
def admin_groups_list(request):
    if request.method == "POST":
        name = (request.POST.get("name") or "").strip()[:GROUP_NAME_MAX]
        if not name:
            messages.error(request, "Укажите название группы.")
            return redirect("dashboard:admin_groups_list")
        try:
            group = Group.objects.create(name=name)
        except IntegrityError:
            messages.error(request, f"Группа «{name}» уже есть.")
            return redirect("dashboard:admin_groups_list")
        _audit(request, name, mode="admin_group_created", group_id=group.id)
        messages.success(request, f"Группа «{name}» создана — отметьте её права.")
        return redirect("dashboard:admin_group_detail", group_id=group.id)

    return render(request, "dashboard/admin_groups_list.html", {
        "page_title": "Группы прав /admin — DOPX Staff",
        "active_tab": "access_roles",
        "groups": groups_with_counts(),
    })


@superuser_required
def admin_group_detail(request, group_id: int):
    group = get_object_or_404(Group, id=group_id)

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "delete":
            name = group.name
            group.delete()
            _audit(request, name, mode="admin_group_deleted", group_id=group_id)
            messages.success(request, f"Группа «{name}» удалена.")
            return redirect("dashboard:admin_groups_list")

        new_name = (request.POST.get("name") or "").strip()[:GROUP_NAME_MAX]
        if new_name and new_name != group.name:
            if Group.objects.filter(name=new_name).exclude(id=group.id).exists():
                messages.error(request, f"Группа «{new_name}» уже есть.")
                return redirect("dashboard:admin_group_detail", group_id=group.id)
            group.name = new_name
            group.save(update_fields=["name"])
        count = set_group_permissions(group, request.POST.getlist("perm"))
        _audit(request, group.name, mode="admin_group_permissions", group_id=group.id, permissions=count)
        messages.success(request, f"«{group.name}»: сохранено прав — {count}.")
        return redirect("dashboard:admin_group_detail", group_id=group.id)

    selected = set(group.permissions.values_list("id", flat=True))
    return render(request, "dashboard/admin_group_detail.html", {
        "page_title": f"Группа: {group.name} — DOPX Staff",
        "active_tab": "access_roles",
        "group": group,
        "apps": permission_matrix(selected),
        "crud_labels": CRUD_LABELS.values(),
        "members": group.user_set.order_by("username"),
        "superuser_only": superuser_only_models(),
    })


@superuser_required
@require_POST
def access_user_admin_groups(request, user_id):
    """Группы /admin/ сотрудника."""
    user = get_object_or_404(get_user_model(), id=user_id, is_staff=True, is_superuser=False)
    ids = [int(x) for x in request.POST.getlist("group") if x.isdigit()]
    groups = list(Group.objects.filter(id__in=ids))
    user.groups.set(groups)
    _audit(request, user.username, mode="admin_groups", groups=[g.name for g in groups])
    messages.success(request, f"«{user.username}»: группы /admin сохранены ({len(groups)}).")
    return redirect("dashboard:access_roles_detail", user_id=user.id)


@superuser_required
@require_POST
def access_grant_staff(request):
    """Выдаёт staff по username или email."""
    ident = (request.POST.get("user") or "").strip()
    User = get_user_model()
    user = User.objects.filter(username__iexact=ident).first() or User.objects.filter(email__iexact=ident).first()
    if not user:
        messages.error(request, f"Пользователь «{ident}» не найден.")
        return redirect("dashboard:access_roles_list")
    if user.is_superuser or user.is_staff:
        messages.info(request, f"«{user.username}» уже сотрудник.")
    else:
        user.is_staff = True
        user.save(update_fields=["is_staff"])
        _audit(request, user.username, mode="staff_granted")
        messages.success(request, f"«{user.username}» теперь сотрудник. По умолчанию — полный доступ к дашборду; ограничьте ниже.")
    return redirect("dashboard:access_roles_detail", user_id=user.id)


@superuser_required
@require_POST
def access_revoke_staff(request, user_id):
    """Снимает staff: закрывает дашборд и /admin/, группы очищаются."""
    user = get_object_or_404(get_user_model(), id=user_id, is_staff=True, is_superuser=False)
    user.is_staff = False
    user.save(update_fields=["is_staff"])
    user.groups.clear()
    user.user_permissions.clear()
    _audit(request, user.username, mode="staff_revoked")
    messages.success(request, f"«{user.username}» больше не сотрудник.")
    return redirect("dashboard:access_roles_list")
