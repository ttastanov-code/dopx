# dashboard/tests_access.py
"""Меню дашборда по правам и группы прав /admin."""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse

from dopx.admin_nav import admin_perm, dashboard_perm

from .admin_access import permission_matrix, set_group_permissions
from .models import StaffAccessGrant
from .nav import build_nav, first_allowed_url

User = get_user_model()


def _staff(name: str, sections: list[str] | None = None):
    user = User.objects.create_user(username=name, email=f"{name}@t.local", password="x", is_staff=True)
    if sections is not None:
        StaffAccessGrant.objects.create(user=user, allowed_sections=sections)
    return user


class DashboardNavTests(TestCase):
    def _keys(self, nav) -> list[str]:
        return [i["key"] for g in nav["groups"] for i in g["items"]]

    def test_limited_user_sees_only_allowed_pages_without_empty_groups(self):
        nav = build_nav(_staff("lim", ["overview", "matches", "scripts"]), "matches")

        self.assertEqual(self._keys(nav), ["overview", "matches", "scripts"])
        self.assertEqual([g["key"] for g in nav["groups"]], ["home", "data", "system"])
        self.assertEqual(nav["current"]["key"], "data")
        self.assertTrue(nav["show_scripts"])

    def test_full_access_groups_and_active_page(self):
        from .models import DASHBOARD_SECTION_KEYS

        user = _staff("full", list(DASHBOARD_SECTION_KEYS))
        user.user_permissions.set(Permission.objects.all())
        nav = build_nav(User.objects.get(pk=user.pk), "experts")
        self.assertEqual(len(nav["groups"]), 5)
        self.assertNotIn("access_roles", self._keys(nav))  # только суперпользователю
        content = next(g for g in nav["groups"] if g["key"] == "content")
        self.assertTrue(content["active"])
        self.assertEqual(nav["current"], content)

    def test_every_section_is_in_menu(self):
        from .models import DASHBOARD_SECTION_KEYS
        from .nav import NAV_GROUPS

        in_menu = {i.key for g in NAV_GROUPS for i in g.items}
        self.assertTrue(set(DASHBOARD_SECTION_KEYS) <= in_menu)

    def test_staff_without_grant_has_no_sections(self):
        # Нет StaffAccessGrant — доступа нет (разделы выдаются явно).
        nav = build_nav(_staff("nogrant"))
        self.assertEqual(nav["groups"], [])
        self.assertFalse(nav["show_scripts"])

    def test_no_sections_no_groups(self):
        nav = build_nav(_staff("none", []))
        self.assertEqual(nav["groups"], [])
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
        # Открытый раздел виден и без права view_ — первым будет антифрод.
        self.assertEqual(first_allowed_url(user), "/staff/dashboard/antifraud/")
        self.assertEqual(first_allowed_url(_staff("one", ["audit"])), "/staff/dashboard/audit/")
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


@override_settings(STAFF_2FA_ENFORCED=False)
class ModelPermissionTests(TestCase):
    """Галочки «просмотр/создание/изменение/удаление» из «Доступов» работают в дашборде."""

    def setUp(self):
        from datetime import timedelta

        from django.utils import timezone

        from engagement.models import ExpertTake
        from leagues.models import League
        from matches.models import Match
        from seasons.models import Season
        from teams.models import Team

        league = League.objects.create(name="КПЛ", country="KZ")
        season = Season.objects.create(league=league, year="2026", is_active=True)
        start = timezone.now() - timedelta(hours=3)
        self.match = Match.objects.create(league=league, season=season, home_team=Team.objects.create(name="А"),
                                          away_team=Team.objects.create(name="Б"), status="finished", start_time=start,
                                          voting_open_until=start + timedelta(days=2))
        self.take = ExpertTake.objects.create(match=self.match, text="Мнение")
        self.user = _staff("editor", ["experts"])
        self.client.force_login(self.user)

    def grant(self, *codenames):
        self.user.user_permissions.add(*Permission.objects.filter(codename__in=codenames))
        self.user = User.objects.get(pk=self.user.pk)  # сброс кэша прав
        self.client.force_login(self.user)

    def test_every_dashboard_url_has_permission_rule(self):
        from django.urls import get_resolver

        from .permissions import SECTION_ONLY, VIEW_PERMS

        dashboard = {p.name for p in get_resolver("dashboard.urls").url_patterns if p.name}
        self.assertFalse(dashboard - set(VIEW_PERMS) - SECTION_ONLY, "добавьте права в dashboard/permissions.py")
        self.assertFalse(set(VIEW_PERMS) - dashboard, "лишние имена в VIEW_PERMS")

    def test_view_only_can_open_but_not_change(self):
        from .nav import build_nav

        # Раздел открыт — смотреть можно и без права view_, менять — нет.
        self.assertIn("experts", [i["key"] for g in build_nav(self.user)["groups"] for i in g["items"]])
        page = self.client.get(reverse("dashboard:experts"))
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, reverse("dashboard:expert_take_toggle", args=[self.take.pk]))
        denied = self.client.post(reverse("dashboard:expert_take_toggle", args=[self.take.pk]))
        self.assertEqual(denied.status_code, 403)
        self.assertContains(denied, "изменение", status_code=403)
        self.assertEqual(self.client.post(reverse("dashboard:expert_take_delete", args=[self.take.pk])).status_code, 403)

        self.grant("change_experttake")
        self.assertEqual(self.client.post(reverse("dashboard:expert_take_toggle", args=[self.take.pk])).status_code, 302)
        self.take.refresh_from_db()
        self.assertFalse(self.take.is_published)


@override_settings(STAFF_2FA_ENFORCED=False)
class MatchEditPermissionTests(TestCase):
    def test_match_edit_needs_change_permission(self):
        from datetime import timedelta

        from django.utils import timezone

        from leagues.models import League
        from matches.models import Match
        from seasons.models import Season
        from teams.models import Team

        league = League.objects.create(name="КПЛ", country="KZ")
        season = Season.objects.create(league=league, year="2026", is_active=True)
        start = timezone.now() - timedelta(hours=3)
        match = Match.objects.create(league=league, season=season, home_team=Team.objects.create(name="А"),
                                     away_team=Team.objects.create(name="Б"), status="finished", home_score=1, away_score=0,
                                     start_time=start, voting_open_until=start + timedelta(days=2))
        user = _staff("viewer", ["matches"])
        self.client.force_login(user)
        url = reverse("dashboard:match_detail", args=[match.pk])
        page = self.client.get(url)
        self.assertContains(page, "Только просмотр")
        self.assertEqual(self.client.post(url, {"status": "finished", "home_score": "5", "away_score": "0"}).status_code, 403)
        match.refresh_from_db()
        self.assertEqual(match.home_score, 1)
        user.user_permissions.add(Permission.objects.get(codename="change_match"))
        self.client.force_login(User.objects.get(pk=user.pk))
        self.assertEqual(self.client.post(url, {"status": "finished", "home_score": "5", "away_score": "0"}).status_code, 302)
        match.refresh_from_db()
        self.assertEqual(match.home_score, 5)


@override_settings(STAFF_2FA_ENFORCED=False)
class PrivilegeEscalationTests(TestCase):
    """Сотрудник с максимумом прав, выдаваемых ролью, не может добраться до суперпользователя."""

    def setUp(self):
        self.boss = User.objects.create_superuser(username="boss", email="boss@t.local", password="x")
        self.mod = _staff("mod", ["users", "scripts", "admin_bot"])
        codes = ["view_user", "add_user", "change_user", "delete_user", "change_group", "view_group",
                 "view_totpdevice", "change_totpdevice", "view_botlink", "change_botlink", "add_botlink"]
        self.mod.user_permissions.set(Permission.objects.filter(codename__in=codes))
        self.client.force_login(self.mod)

    def test_admin_cannot_touch_staff_accounts(self):
        url = reverse("admin:users_user_change", args=[self.boss.pk])
        self.client.post(url, {"username": "boss", "email": "evil@t.local"})
        self.boss.refresh_from_db()
        self.assertEqual(self.boss.email, "boss@t.local")
        self.assertNotIn('name="email"', self.client.get(url).content.decode())
        self.client.post(reverse("admin:users_user_delete", args=[self.boss.pk]), {"post": "yes"})
        self.assertTrue(User.objects.filter(pk=self.boss.pk).exists())
        resp = self.client.post(reverse("admin:users_user_changelist"),
                                {"action": "delete_selected", "_selected_action": [self.boss.pk], "post": "yes"})
        self.assertTrue(User.objects.filter(pk=self.boss.pk).exists(), resp.status_code)

    def test_admin_groups_devices_botlinks_superuser_only(self):
        for name in ("auth_group_changelist", "otp_totp_totpdevice_changelist", "adminbot_botlink_changelist",
                     "otp_static_staticdevice_changelist", "axes_accessattempt_changelist"):
            self.assertEqual(self.client.get(reverse(f"admin:{name}")).status_code, 403, name)
        self.assertEqual(self.client.get(reverse("admin:auth_group_add")).status_code, 403)

    def test_destructive_script_superuser_only(self):
        from dashboard.models import ManagementCommandRun

        self.client.post(reverse("dashboard:scripts_trigger"), {
            "command_name": "reset_user_activity", "apply": "on", "delete_staff": "on", "confirm_text": "reset_user_activity"})
        self.assertFalse(ManagementCommandRun.objects.exists())
        self.assertContains(self.client.get(reverse("dashboard:scripts")), "запускает только суперпользователь")

    def test_dashboard_cannot_ban_or_manage_access(self):
        self.client.post(reverse("dashboard:user_toggle_ban", args=[self.boss.pk]))
        self.boss.refresh_from_db()
        self.assertTrue(self.boss.is_active)
        self.assertEqual(self.client.get(reverse("dashboard:access_roles_list")).status_code, 403)
        self.client.post(reverse("dashboard:access_grant_staff"), {"user": "someone"})
        self.client.post(reverse("dashboard:access_roles_detail", args=[self.mod.pk]), {"action": "personal", "section_access_roles": "on"})
        self.assertNotIn("access_roles", StaffAccessGrant.objects.get(user=self.mod).allowed_sections)
