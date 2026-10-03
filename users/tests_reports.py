# users/tests_reports.py
"""Жалобы: приём с сайта, лимиты, меры в дашборде и права."""
from __future__ import annotations

from django.contrib.auth.models import Permission
from django.test import TestCase, override_settings
from django.urls import reverse

from dashboard.models import StaffAccessGrant, StaffActionLog
from engagement.models import FriendLeague
from leagues.models import League
from notifications.models import Notification
from seasons.models import Season
from users import reports
from users.models import User, UserReport


@override_settings(STAFF_2FA_ENFORCED=False)
class UserReportTests(TestCase):
    def setUp(self):
        self.fan = User.objects.create_user(username="fan", email="fan@t.local", password="x")
        self.bad = User.objects.create_user(username="badname", email="bad@t.local", password="x", bio="гадость")
        season = Season.objects.create(league=League.objects.create(name="КПЛ", country="KZ"), year="2026", is_active=True)
        self.league = FriendLeague.objects.create(name="Плохое имя", owner=self.bad, season=season)
        self.boss = User.objects.create_superuser(username="boss", email="boss@t.local", password="x")

    def test_site_report_and_limits(self):
        self.client.force_login(self.fan)
        url = reverse("users:report", args=["user", "badname"])
        self.client.post(url, {"reason": "name", "comment": "мат в нике", "next": "/users/u/badname/"})
        self.client.post(url, {"reason": "name"})  # повтор не плодим
        self.assertEqual(UserReport.objects.filter(target_user=self.bad).count(), 1)
        self.client.post(reverse("users:report", args=["league", self.league.invite_code]), {"reason": "name"})
        self.assertEqual(UserReport.objects.get(friend_league=self.league).target_name, "Плохое имя")
        # Без причины, на себя и чужой next — без жалобы и без редиректа наружу.
        response = self.client.post(url, {"reason": "", "next": "https://evil.example/"})
        self.assertEqual(response.url, "/")
        with self.assertRaises(reports.ReportError):
            reports.create(self.fan, reason="spam", target_user=self.fan)
        self.assertEqual(self.client.post(reverse("users:report", args=["x", "y"])).status_code, 404)
        # Профиль показывает кнопку только вошедшим.
        self.assertContains(self.client.get(reverse("users:public_profile", args=["badname"])), "Пожаловаться")
        self.client.logout()
        self.assertNotContains(self.client.get(reverse("users:public_profile", args=["badname"])), "Пожаловаться")

    def test_daily_limit(self):
        for i in range(reports.DAILY_LIMIT):
            other = User.objects.create_user(username=f"u{i}", email=f"u{i}@t.local", password="x")
            reports.create(self.fan, reason="spam", target_user=other)
        with self.assertRaises(reports.ReportError):
            reports.create(self.fan, reason="spam", target_user=self.bad)

    def test_dashboard_actions(self):
        r1 = reports.create(self.fan, reason="name", target_user=self.bad)
        other = User.objects.create_user(username="other", email="o@t.local", password="x")
        reports.create(other, reason="bio", target_user=self.bad)
        self.client.force_login(self.boss)
        page = self.client.get(reverse("dashboard:reports"))
        self.assertContains(page, "Сбросить ник")
        self.assertContains(page, "Очистить «О себе»")
        self.client.post(reverse("dashboard:report_action", args=[r1.pk]), {"action": "reset_username"})
        self.bad.refresh_from_db()
        self.assertTrue(self.bad.username.startswith("fan"))
        # Обе жалобы на этого человека закрыты одной мерой.
        self.assertFalse(UserReport.objects.filter(status="new").exists())
        self.assertTrue(Notification.objects.filter(user=self.bad, notification_type="system").exists())
        self.assertTrue(StaffActionLog.objects.filter(action="user_report_handled").exists())
        self.assertContains(self.client.get(reverse("dashboard:reports") + "?status=done"), "Сбросить ник")

    def test_delete_league_keeps_history(self):
        r = reports.create(self.fan, reason="name", league=self.league)
        self.client.force_login(self.boss)
        self.client.post(reverse("dashboard:report_action", args=[r.pk]), {"action": "delete_league"})
        self.assertFalse(FriendLeague.objects.exists())
        r.refresh_from_db()
        self.assertEqual((r.status, r.action, r.target_label), ("resolved", "delete_league", "лига «Плохое имя»"))

    def test_staff_target_and_permissions(self):
        staff_target = User.objects.create_user(username="mod", email="m@t.local", password="x", is_staff=True)
        r = reports.create(self.fan, reason="name", target_user=staff_target)
        self.assertEqual([k for k, _ in reports.allowed_actions(r, self.boss)], ["reject"])
        # Модератор с правом на жалобы, но без users.change_user — только «Отклонить».
        mod = User.objects.create_user(username="mod2", email="m2@t.local", password="x", is_staff=True)
        StaffAccessGrant.objects.create(user=mod, allowed_sections=["reports"])
        mod.user_permissions.add(Permission.objects.get(codename="change_userreport"),
                                 Permission.objects.get(codename="view_userreport"))
        mod = User.objects.get(pk=mod.pk)
        r2 = reports.create(self.fan, reason="name", target_user=self.bad)
        self.assertEqual([k for k, _ in reports.allowed_actions(r2, mod)], ["reject"])
        self.client.force_login(mod)
        self.client.post(reverse("dashboard:report_action", args=[r2.pk]), {"action": "ban"})
        self.bad.refresh_from_db()
        self.assertTrue(self.bad.is_active)

    def test_bot_queue(self):
        from adminbot import handlers

        self.assertIn("Новых жалоб нет", handlers.reports_view(self.boss)[0])
        r = reports.create(self.fan, reason="bio", comment="оскорбления", target_user=self.bad)
        text, rows = handlers.reports_view(self.boss)
        self.assertIn("оскорбления", text)
        labels = [b[0] for row in rows for b in row]
        self.assertIn("🧹 Очистить «О себе»", labels)
        self.assertNotIn("⛔", " ".join(labels))
        self.assertEqual(handlers.counts()["reports"], 1)
        self.assertIn("очищено", handlers.act_report(self.boss, str(r.pk), "clear_bio"))
        self.bad.refresh_from_db()
        self.assertEqual(self.bad.bio, "")
