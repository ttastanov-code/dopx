# dashboard/admin_access.py
"""Группы и права Django /admin/ для раздела «Роли доступа»."""
from __future__ import annotations

from django.apps import apps
from django.contrib.auth.models import Group, Permission

# Стандартные действия моделей в порядке колонок матрицы.
CRUD_ACTIONS = ("view", "add", "change", "delete")
CRUD_LABELS = {"view": "Просмотр", "add": "Создание", "change": "Изменение", "delete": "Удаление"}

# Безопасность и служебное: права только у суперпользователя, в матрице не показываем.
HIDDEN_APPS = {"contenttypes", "sessions", "admin", "auth", "axes", "otp_totp", "otp_static", "captcha"}

# Даёт возможность повысить себе права.
DANGEROUS_MODELS = {("users", "user")}


def _grantable_model_admins() -> dict:
    """Модели, которые реально есть в /admin/ и доступ к которым выдаётся группами."""
    from django.contrib import admin

    from core.admin_mixins import SuperuserOnlyAdminMixin

    return {
        model: model_admin for model, model_admin in admin.site._registry.items()
        if model._meta.app_label not in HIDDEN_APPS and not isinstance(model_admin, SuperuserOnlyAdminMixin)
    }


def superuser_only_models() -> list[str]:
    """Модели /admin/, закрытые для групп: ими управляют разделы дашборда."""
    from django.contrib import admin

    from core.admin_mixins import SuperuserOnlyAdminMixin

    return sorted(
        str(m._meta.verbose_name_plural) for m, ma in admin.site._registry.items()
        if isinstance(ma, SuperuserOnlyAdminMixin)
    )


def _grantable_permissions():
    from django.contrib.contenttypes.models import ContentType

    cts = ContentType.objects.get_for_models(*_grantable_model_admins().keys()).values()
    return Permission.objects.select_related("content_type").filter(content_type__in=list(cts))


def _app_label(app_label: str) -> str:
    try:
        return str(apps.get_app_config(app_label).verbose_name)
    except LookupError:
        return app_label


def permission_matrix(selected_ids: set[int]) -> list[dict]:
    """Права, сгруппированные: приложение -> модель -> CRUD-колонки + прочие права."""
    perms = _grantable_permissions().order_by("content_type__app_label", "content_type__model", "codename")
    by_app: dict[str, dict] = {}
    for p in perms:
        ct = p.content_type
        app = by_app.setdefault(ct.app_label, {"key": ct.app_label, "label": _app_label(ct.app_label), "models": {}})
        model_cls = ct.model_class()
        row = app["models"].setdefault(ct.model, {
            "key": ct.model,
            "label": str(model_cls._meta.verbose_name_plural) if model_cls else ct.model,
            "dangerous": (ct.app_label, ct.model) in DANGEROUS_MODELS,
            "crud": {a: None for a in CRUD_ACTIONS},
            "extra": [],
        })
        cell = {"id": p.id, "name": p.name, "checked": p.id in selected_ids}
        action = p.codename.split("_", 1)[0]
        if p.codename == f"{action}_{ct.model}" and action in CRUD_ACTIONS:
            row["crud"][action] = cell
        else:
            row["extra"].append(cell)

    result = []
    for app in sorted(by_app.values(), key=lambda a: a["label"]):
        models = sorted(app["models"].values(), key=lambda m: m["label"])
        for m in models:
            m["crud_cells"] = [m["crud"][a] for a in CRUD_ACTIONS]
        checked = sum(1 for m in models for c in [*m["crud_cells"], *m["extra"]] if c and c["checked"])
        result.append({**app, "models": models, "checked_count": checked})
    return result


def set_group_permissions(group: Group, raw_ids: list[str]) -> int:
    """Заменяет права группы; неизвестные id отбрасываются. Возвращает число прав."""
    ids = {int(x) for x in raw_ids if str(x).isdigit()}
    perms = list(_grantable_permissions().filter(id__in=ids))
    group.permissions.set(perms)
    return len(perms)


def groups_with_counts():
    from django.db.models import Count

    return Group.objects.annotate(
        perm_count=Count("permissions", distinct=True), user_count=Count("user", distinct=True),
    ).order_by("name")
