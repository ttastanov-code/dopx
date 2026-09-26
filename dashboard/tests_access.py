# dashboard/tests_access.py
"""Меню дашборда по правам и группы прав /admin."""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.test import RequestFactory, TestCase

from dopx.admin_nav import admin_perm, dashboard_perm

from .admin_access import permission_matrix, set_group_permissions
from .models import StaffAccessGrant
from .nav import SINGLE_ROW_MAX_ITEMS, build_nav, first_allowed_url

User = get_user_model()


def _staff(name: str, sections: list[str] | None = None):
    user = User.objects.create_user(username=name, email=f"{name}@t.local", password="x", is_staff=True)
    if sections is not None:
        StaffAccessGrant.objects.create(user=user, allowed_sections=sections)
    return user


class DashboardNavTests(TestCase):
    def _keys(self, nav) -> list[str]:
        return [i["key"] for row in nav["rows"] for g in row for i in g]

    def test_limited_user_gets_single_row_without_empty_groups(self):
        nav = build_nav(_staff("lim", ["overview", "matches", "scripts"]))

        self.assertEqual(len(nav["rows"]), 1)
        self.assertEqual(self._keys(nav), ["overview", "matches"])
        self.assertTrue(all(nav["rows"][0]))
        self.assertTrue(nav["show_scripts"])

    def test_full_access_uses_two_rows(self):
        from .models import DASHBOARD_SECTION_KEYS

        nav = build_nav(_staff("full", list(DASHBOARD_SECTION_KEYS)))
        self.assertGreater(nav["total"], SINGLE_ROW_MAX_ITEMS)
        self.assertEqual(len(nav["rows"]), 2)

    def test_staff_without_grant_has_no_sections(self):
        # Нет StaffAccessGrant — доступа нет (разделы выдаются явно).
        nav = build_nav(_staff("nogrant"))
        self.assertEqual(nav["rows"], [])
        self.assertFalse(nav["show_scripts"])

    def test_no_sections_no_rows(self):
        nav = build_nav(_staff("none", []))
        self.assertEqual(nav["rows"], [])
        self.assertFalse(nav["show_scripts"])
        self.assertFalse(nav["show_admin"])


class AdminGroupPermissionTests(TestCase):
    def setUp(self):
        self.group = Group.objects.create(name="Модератор")
        self.view_user = Permission.objects.get(codename="view_user", content_type__app_label="users")

    def test_set_permissions_ignores_garbage_and_hidden_apps(self):
        session_perm = Permission.objects.filter(content_type__app_label="sessions").first()
        raw = [str(self.view_user.id), "abc", "999999"] + ([str(session_perm.id)] if session_perm else [])

        count = set_group_permissions(self.group, raw)

        self.assertEqual(count, 1)
        self.assertEqual(list(self.group.permissions.all()), [self.view_user])

    def test_matrix_marks_checked_and_dangerous(self):
        apps = permission_matrix({self.view_user.id})
        users_app = next(a for a in apps if a["key"] == "users")
        user_row = next(m for m in users_app["models"] if m["key"] == "user")

        self.assertTrue(user_row["dangerous"])
        self.assertTrue(user_row["crud_cells"][0]["checked"])
        self.assertFalse(user_row["crud_cells"][3]["checked"])
        self.assertEqual(users_app["checked_count"], 1)
        self.assertFalse(any(a["key"] == "sessions" for a in apps))

    def test_group_permissions_reach_user(self):
        set_group_permissions(self.group, [str(self.view_user.id)])
        user = _staff("mod")
        user.groups.add(self.group)
        user = User.objects.get(id=user.id)

        self.assertTrue(user.has_perm("users.view_user"))
        self.assertTrue(build_nav(user)["show_admin"])


class AccessExitsTests(TestCase):
    def test_first_allowed_url_skips_closed_sections(self):
        user = _staff("two", ["antifraud", "audit"])
        self.assertEqual(first_allowed_url(user), "/staff/dashboard/antifraud/")
        self.assertIsNone(first_allowed_url(_staff("zero", [])))


class AdminSidebarPermissionTests(TestCase):
    def _request(self, user):
        request = RequestFactory().get("/admin/")
        request.user = user
        return request

    def test_model_item_hidden_without_permission(self):
        user = _staff("noperm")
        self.assertFalse(admin_perm("users_user")(self._request(user)))

        group = Group.objects.create(name="Смотрящий")
        group.permissions.add(Permission.objects.get(codename="view_user", content_type__app_label="users"))
        user.groups.add(group)
        user = User.objects.get(id=user.id)
        self.assertTrue(admin_perm("users_user")(self._request(user)))

    def test_dashboard_item_follows_sections(self):
        user = _staff("sec", ["audit"])
        self.assertTrue(dashboard_perm("audit")(self._request(user)))
        self.assertFalse(dashboard_perm("scripts")(self._request(user)))


class AdminConsistencyTests(TestCase):
    """Чекап: права в матрице = модели в /admin/ = пункты сайдбара."""

    def _sidebar_links(self) -> set[str]:
        from django.conf import settings

        return {
            str(item["link"]) for group in settings.UNFOLD["SIDEBAR"]["navigation"] for item in group["items"]
        }

    def test_every_admin_model_is_in_sidebar(self):
        from django.contrib import admin
        from django.urls import reverse

        links = self._sidebar_links()
        missing = [
            f"{m._meta.app_label}.{m._meta.model_name}" for m in admin.site._registry
            if reverse(f"admin:{m._meta.app_label}_{m._meta.model_name}_changelist") not in links
        ]
        self.assertEqual(missing, [])

    def test_matrix_has_only_grantable_admin_models(self):
        from django.contrib import admin

        apps = permission_matrix(set())
        keys = {(a["key"], m["key"]) for a in apps for m in a["models"]}
        registered = {(m._meta.app_label, m._meta.model_name) for m in admin.site._registry}

        self.assertTrue(keys <= registered)
        for hidden in [("otp_totp", "totpdevice"), ("auth", "group"), ("core", "platformsetting"),
                       ("dashboard", "staffaccessgrant"), ("players", "potentialduplicateplayer")]:
            self.assertNotIn(hidden, keys)
        self.assertIn(("round_squad", "roundbestxi"), keys)

    def test_superuser_only_model_closed_even_with_permission(self):
        from django.contrib import admin

        from core.models import PlatformSetting

        user = _staff("sneaky")
        user.user_permissions.add(Permission.objects.get(codename="change_platformsetting"))
        user = User.objects.get(id=user.id)
        request = RequestFactory().get("/admin/")
        request.user = user

        self.assertFalse(admin.site._registry[PlatformSetting].has_change_permission(request))

    def test_user_admin_privilege_fields_readonly_for_staff(self):
        from django.contrib import admin

        model_admin = admin.site._registry[User]
        request = RequestFactory().get("/admin/")
        request.user = _staff("editor")
        self.assertIn("is_superuser", model_admin.get_readonly_fields(request))

        request.user = User.objects.create_superuser("boss", "boss@t.local", "x")
        self.assertNotIn("is_superuser", model_admin.get_readonly_fields(request))
