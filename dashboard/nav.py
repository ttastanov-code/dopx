# dashboard/nav.py
"""Состав меню дашборда: разделы -> страницы; ключ страницы = ключ доступа = active_tab."""
from __future__ import annotations

from django.urls import reverse

from .access import user_can_access_section
from .nav_data import NAV_GROUPS, NavGroup, NavItem  # noqa: F401


def _can_open(user, item: NavItem) -> bool:
    """Раздел открыт и хватает прав на просмотр его страницы (dashboard/permissions.py)."""
    from .permissions import missing_perms

    return user_can_access_section(user, item.key) and not missing_perms(user, item.url_name.split(":")[1], "GET")


def build_nav(user, active_tab: str = "") -> dict:
    """Видимые пользователю разделы и страницы; пустые разделы выброшены."""
    groups = []
    for group in NAV_GROUPS:
        items = [
            {"key": i.key, "url": reverse(i.url_name), "icon": i.icon, "label": i.label, "hint": i.hint,
             "warn": i.warn, "active": i.key == active_tab}
            for i in group.items if _can_open(user, i)
        ]
        if items:
            groups.append({"key": group.key, "icon": group.icon, "label": group.label, "items": items,
                           "url": items[0]["url"], "active": any(i["active"] for i in items)})
    current = next((g for g in groups if g["active"]), None)
    items = {i["key"]: i for g in groups for i in g["items"]}
    return {
        "groups": groups,
        # Палитра ⌘K: все открытые страницы и поиск — только по доступным разделам.
        "palette": {
            "items": [{"label": i["label"], "hint": i["hint"], "group": g["label"], "icon": i["icon"], "url": i["url"]}
                      for g in groups for i in g["items"]],
            "search": {key: items[key]["url"] for key in ("matches", "users") if key in items},
            "admin": _admin_links(user),
        },
        "current_item": next((i for g in groups for i in g["items"] if i["active"]), None),
        "current": current,
        "total": sum(len(g["items"]) for g in groups),
        "show_scripts": user_can_access_section(user, "scripts"),
        # /admin/ без прав показывает пустую страницу — иконку прячем.
        "show_admin": user.is_superuser or bool(user.get_all_permissions()),
    }


def first_allowed_url(user) -> str | None:
    """Первый открытый сотруднику раздел дашборда — куда вести со страницы 403."""
    for group in NAV_GROUPS:
        for item in group.items:
            if _can_open(user, item):
                return reverse(item.url_name)
    return None


def _admin_links(user) -> list[dict]:
    """Таблицы Django admin, которые сотрудник может открыть, — для палитры ⌘K."""
    from django.contrib import admin
    from django.urls import NoReverseMatch

    rows = []
    for model in admin.site._registry:
        meta = model._meta
        if not (user.has_perm(f"{meta.app_label}.view_{meta.model_name}") or user.has_perm(f"{meta.app_label}.change_{meta.model_name}")):
            continue
        try:
            url = reverse(f"admin:{meta.app_label}_{meta.model_name}_changelist")
        except NoReverseMatch:
            continue
        rows.append({"label": str(meta.verbose_name_plural).capitalize(), "hint": str(meta.app_config.verbose_name), "url": url})
    return sorted(rows, key=lambda r: r["label"])
