from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase, override_settings
from django.urls import reverse

from . import roles
from .access import user_can_access_section
from .models import StaffAccessGrant, StaffRole
from .permissions import VIEW_PERMS

User = get_user_model()


def fresh(user):
    return User.objects.get(pk=user.pk)


class RolesLogicTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="mod", email="m@t.local", password="x", is_staff=True)

    def test_levels_set_permissions_and_sections(self):
        group = Group.objects.create(name="Модератор")
        roles.save_role(group, {"antifraud": "work", "overview": "on", "users": "view", "bogus": "full", "audit": "full"})
        role = StaffRole.objects.get(group=group)
        self.assertEqual(role.levels, {"antifraud": "work", "overview": "on", "users": "view"})  # мусор отброшен
        codes = set(group.permissions.values_list("codename", flat=True))
        self.assertEqual(codes, {"view_suspiciousactivityflag", "add_suspiciousactivityflag", "change_suspiciousactivityflag", "view_user"})
        self.user.groups.add(group)
        u = fresh(self.user)
        self.assertTrue(user_can_access_section(u, "antifraud"))
        self.assertFalse(user_can_access_section(u, "experts"))
        self.assertTrue(u.has_perm("users.change_suspiciousactivityflag"))
        self.assertFalse(u.has_perm("users.delete_suspiciousactivityflag"))
        self.assertFalse(u.has_perm("users.change_user"))

    def test_lowering_level_removes_permissions(self):
        group = Group.objects.create(name="Р")
        roles.save_role(group, {"experts": "full"})
        roles.save_role(group, {"experts": "view"})
        self.assertEqual(set(group.permissions.values_list("codename", flat=True)), {"view_expert", "view_experttake", "view_expertinvite"})

    def test_presets_and_access_roles_never_grantable(self):
        for key in roles.PRESETS:
            group = roles.create_from_preset(key)
            self.assertNotIn("access_roles", group.staff_role.levels)
        self.assertEqual(roles.create_from_preset("moderator").name, "Модератор 2")  # без конфликта имён

    def test_summary_combines_roles_and_personal(self):
        a, b = Group.objects.create(name="A"), Group.objects.create(name="B")
        roles.save_role(a, {"experts": "view"})
        roles.save_role(b, {"experts": "full", "traffic": "on"})
        self.user.groups.add(a, b)
        StaffAccessGrant.objects.create(user=self.user, allowed_sections=["ads"])
        rows = {r["key"]: r for r in roles.user_access_summary(self.user)}
        self.assertEqual(rows["experts"]["level"], "full")
        self.assertEqual(rows["experts"]["sources"], ["A", "B"])
        self.assertEqual(rows["ads"]["level"], "view")   # индивидуально — только просмотр
        self.assertTrue(user_can_access_section(fresh(self.user), "ads"))

    def test_infer_levels_for_old_groups(self):
        group = Group.objects.create(name="Старая")
        group.permissions.set(roles.permissions_for({"ads": "work"}))
        self.assertEqual(roles.infer_levels(group), {"ads": "work"})

    def test_every_dashboard_permission_is_grantable_by_some_role(self):
        grantable = {m for models in roles.SECTION_MODELS.values() for m in models}
        for name, rule in VIEW_PERMS.items():
            for perm in rule["GET"] + rule["POST"]:
                app, codename = perm.split(".")
                model = codename.split("_", 1)[1]
                self.assertIn(f"{app}.{model}", grantable, f"{name}: право {perm} нельзя выдать ролью")


@override_settings(STAFF_2FA_ENFORCED=False)
class AccessPagesTests(TestCase):
    def setUp(self):
        self.boss = User.objects.create_superuser(username="boss", email="b@t.local", password="x")
        self.staff = User.objects.create_user(username="mod", email="m@t.local", password="x", is_staff=True)
        self.client.force_login(self.boss)

    def test_role_flow(self):
        home = self.client.get(reverse("dashboard:access_roles_list"))
        self.assertContains(home, "нет ролей")
        resp = self.client.post(reverse("dashboard:access_roles_list"), {"preset": "editor"})
        group = Group.objects.get(name="Редактор контента")
        self.assertRedirects(resp, reverse("dashboard:admin_group_detail", args=[group.id]), fetch_redirect_response=False)
        editor = self.client.get(reverse("dashboard:admin_group_detail", args=[group.id]))
        self.assertContains(editor, 'name="level_experts"')
        self.client.post(reverse("dashboard:admin_group_detail", args=[group.id]),
                         {"name": "Редакция", "description": "Тексты", "level_experts": "work", "level_overview": "on"})
        group.refresh_from_db()
        self.assertEqual(group.name, "Редакция")
        self.assertEqual(group.staff_role.levels, {"experts": "work", "overview": "on"})
        self.client.post(reverse("dashboard:access_roles_detail", args=[self.staff.id]), {"role": [group.id]})
        u = fresh(self.staff)
        self.assertTrue(user_can_access_section(u, "experts"))
        page = self.client.get(reverse("dashboard:access_roles_detail", args=[self.staff.id]))
        self.assertContains(page, "Работа")
        self.client.post(reverse("dashboard:admin_group_detail", args=[group.id]), {"action": "delete"})
        self.assertFalse(user_can_access_section(fresh(self.staff), "experts"))

    def test_staff_without_roles_sees_nothing_and_non_superuser_blocked(self):
        self.assertFalse(user_can_access_section(fresh(self.staff), "overview"))
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(reverse("dashboard:access_roles_list")).status_code, 403)
