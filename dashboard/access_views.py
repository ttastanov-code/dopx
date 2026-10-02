# dashboard/access_views.py
"""«Доступы»: сотрудники и роли в одном месте. Роль задаёт разделы и права на данные сразу (dashboard/roles.py).
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
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from . import roles
from .audit import log_staff_action
from .models import DASHBOARD_SECTIONS, AuditAction, StaffAccessGrant, StaffRole

GROUP_NAME_MAX = 150


def superuser_required(view):
    @staff_member_required
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_superuser:
            raise PermissionDenied("Управление доступами — только для суперпользователей.")
        return view(request, *args, **kwargs)
    return wrapper


def _audit(request, target: str, **details) -> None:
    log_staff_action(request, AuditAction.ACCESS_GRANT_UPDATED, target=target, details=details)


def _role_cards():
    labels = dict(DASHBOARD_SECTIONS)
    cards = []
    for g in Group.objects.annotate(members=Count("user")).select_related("staff_role").order_by("name"):
        role = getattr(g, "staff_role", None)
        levels = role.levels if role else roles.infer_levels(g)
        cards.append({"group": g, "members": g.members, "description": role.description if role else "",
                      "legacy": role is None,
                      "chips": [(labels.get(k, k), roles.LEVEL_HINTS.get(v, v), v) for k, v in levels.items()]})
    return cards


@superuser_required
def access_home(request):
    """Сотрудники и роли. POST: новая роль (пустая или из шаблона)."""
    if request.method == "POST":
        preset = request.POST.get("preset")
        if preset in roles.PRESETS:
            group = roles.create_from_preset(preset)
            _audit(request, group.name, mode="role_created", preset=preset)
            messages.success(request, f"Роль «{group.name}» создана из шаблона — проверьте уровни и сохраните.")
            return redirect("dashboard:admin_group_detail", group_id=group.id)
        name = (request.POST.get("name") or "").strip()[:GROUP_NAME_MAX]
        if not name:
            messages.error(request, "Укажите название роли.")
            return redirect(reverse("dashboard:access_roles_list") + "?tab=roles")
        try:
            group = Group.objects.create(name=name)
        except IntegrityError:
            messages.error(request, f"Роль «{name}» уже есть.")
            return redirect(reverse("dashboard:access_roles_list") + "?tab=roles")
        roles.save_role(group, {})
        _audit(request, name, mode="role_created")
        return redirect("dashboard:admin_group_detail", group_id=group.id)

    tab = request.GET.get("tab", "staff")
    staff = []
    for u in get_user_model().objects.filter(is_staff=True).prefetch_related("groups").order_by("-is_superuser", "username"):
        staff.append({"user": u, "roles": list(u.groups.all()),
                      "sections": len(roles.user_access_summary(u)) if not u.is_superuser else None})
    return render(request, "dashboard/access_home.html", {
        "page_title": "Доступы — DOPX Staff",
        "active_tab": "access_roles",
        "tab": tab,
        "staff": staff,
        "roles": _role_cards(),
        "presets": [(k, name, desc) for k, (name, desc, _l) in roles.PRESETS.items()],
    })


@superuser_required
def role_detail(request, group_id: int):
    """Редактор роли: уровень по каждому разделу, права группы выставляются сами."""
    group = get_object_or_404(Group, id=group_id)
    role = StaffRole.objects.filter(group=group).first()
    if request.method == "POST":
        if request.POST.get("action") == "delete":
            name = group.name
            group.delete()
            _audit(request, name, mode="role_deleted", group_id=group_id)
            messages.success(request, f"Роль «{name}» удалена. У её участников пропали эти разделы и права.")
            return redirect(reverse("dashboard:access_roles_list") + "?tab=roles")
        new_name = (request.POST.get("name") or "").strip()[:GROUP_NAME_MAX]
        if new_name and new_name != group.name:
            if Group.objects.filter(name=new_name).exclude(id=group.id).exists():
                messages.error(request, f"Роль «{new_name}» уже есть.")
                return redirect("dashboard:admin_group_detail", group_id=group.id)
            group.name = new_name
            group.save(update_fields=["name"])
        raw = {key: request.POST.get(f"level_{key}", "off") for _g, items in roles.sections_layout() for key, _l, _h in items}
        role = roles.save_role(group, raw, request.POST.get("description", ""))
        _audit(request, group.name, mode="role_saved", group_id=group.id, levels=role.levels)
        messages.success(request, f"Роль «{group.name}» сохранена. Изменения уже действуют у всех её сотрудников.")
        return redirect("dashboard:admin_group_detail", group_id=group.id)

    levels = role.levels if role else roles.infer_levels(group)
    layout = []
    for group_label, items in roles.sections_layout():
        rows = [{"key": key, "label": label, "hint": hint, "level": levels.get(key, "off"),
                 "choices": roles.levels_for(key), "sensitive": key in roles.SENSITIVE} for key, label, hint in items]
        layout.append((group_label, rows))
    return render(request, "dashboard/access_role.html", {
        "page_title": f"Роль: {group.name} — DOPX Staff",
        "active_tab": "access_roles",
        "group": group,
        "role": role,
        "layout": layout,
        "level_hints": roles.LEVEL_HINTS,
        "members": group.user_set.order_by("username"),
    })


@superuser_required
def access_user(request, user_id):
    """Сотрудник: роли (основное), индивидуальные разделы (редко), итоговый доступ человеческим языком."""
    User = get_user_model()
    target = get_object_or_404(User, id=user_id, is_staff=True)
    grant = StaffAccessGrant.objects.filter(user=target).first()
    if request.method == "POST" and not target.is_superuser:
        if request.POST.get("action") == "personal":
            selected = [k for k, _l in DASHBOARD_SECTIONS if request.POST.get(f"section_{k}") == "on"]
            StaffAccessGrant.objects.update_or_create(user=target, defaults={"allowed_sections": selected, "updated_by": request.user})
            _audit(request, target.username, mode="personal_sections", sections=selected)
            messages.success(request, "Индивидуальные разделы сохранены.")
        else:
            ids = [int(x) for x in request.POST.getlist("role") if x.isdigit()]
            groups = list(Group.objects.filter(id__in=ids))
            target.groups.set(groups)
            _audit(request, target.username, mode="roles", roles=[g.name for g in groups])
            messages.success(request, f"«{target.username}»: роли сохранены ({len(groups)}).")
        return redirect("dashboard:access_roles_detail", user_id=target.id)

    user_role_ids = set(target.groups.values_list("id", flat=True))
    personal = set(grant.allowed_sections) if grant else set()
    return render(request, "dashboard/access_user.html", {
        "page_title": f"Доступ: {target.username} — DOPX Staff",
        "active_tab": "access_roles",
        "target": target,
        "roles": [{**c, "checked": c["group"].id in user_role_ids} for c in _role_cards()],
        "summary": roles.user_access_summary(target),
        "personal": [{"key": k, "label": l, "checked": k in personal} for k, l in DASHBOARD_SECTIONS if k != "access_roles"],
        "personal_count": len(personal),
        "direct_perm_count": target.user_permissions.count(),
        "has_bot": hasattr(target, "bot_link"),
    })


def admin_groups_list(request):
    return redirect(reverse("dashboard:access_roles_list") + "?tab=roles")


@superuser_required
@require_POST
def access_grant_staff(request):
    """Делает пользователя сотрудником (по логину или email) и сразу ведёт к выбору ролей."""
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
        messages.success(request, f"«{user.username}» теперь сотрудник. Выберите ему роль.")
    return redirect("dashboard:access_roles_detail", user_id=user.id)


@superuser_required
@require_POST
def access_revoke_staff(request, user_id):
    """Снимает staff: закрывает дашборд, /admin/ и бот; роли и индивидуальные права очищаются."""
    user = get_object_or_404(get_user_model(), id=user_id, is_staff=True, is_superuser=False)
    user.is_staff = False
    user.save(update_fields=["is_staff"])
    user.groups.clear()
    user.user_permissions.clear()
    StaffAccessGrant.objects.filter(user=user).delete()
    _audit(request, user.username, mode="staff_revoked")
    messages.success(request, f"«{user.username}» больше не сотрудник.")
    return redirect("dashboard:access_roles_list")
