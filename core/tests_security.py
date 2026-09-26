# core/tests_security.py
"""Безопасность: вложения обращений, редиректы после входа и 2FA, выход по POST,
токен подтверждения почты, доступ к дашборду, сид-команды, дубли email, XP-остаток,
правка оценки после завершения.
"""
from datetime import timedelta
from io import BytesIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from core.uploads import AttachmentRejected, validate_attachment
from core.utils import canonical_email
from dashboard.models import StaffAccessGrant
from evaluations.models import ContextEvaluation, EvaluationSession, TeamEvaluation
from leagues.models import League
from matches.models import Match
from seasons.models import Season
from teams.models import Team
from users.forms import UserProfileForm
from users.models import UserXP

User = get_user_model()


def _png(name="shot.png"):
    buf = BytesIO()
    Image.new("RGB", (4, 4), "red").save(buf, format="PNG")
    return SimpleUploadedFile(name, buf.getvalue(), content_type="image/png")


class AttachmentValidationTests(TestCase):
    def test_html_rejected_even_with_image_name(self):
        fake = SimpleUploadedFile("shot.png", b"<script>alert(1)</script>", content_type="image/png")
        with self.assertRaises(AttachmentRejected):
            validate_attachment(fake)

    def test_png_gets_random_safe_name(self):
        name = validate_attachment(_png("../../evil.html"))
        self.assertRegex(name, r"^[0-9a-f]{32}\.png$")

    def test_pdf_accepted(self):
        pdf = SimpleUploadedFile("doc.pdf", b"%PDF-1.4\n...", content_type="application/pdf")
        self.assertTrue(validate_attachment(pdf).endswith(".pdf"))

    def test_attachment_download_staff_only(self):
        from notifications.models import ContactSubmission

        sub = ContactSubmission.objects.create(guest_email="a@b.kz", subject="s", message="x" * 30)
        sub.attachment.save("f.png", _png(), save=True)
        url = reverse("notifications:contact_attachment", args=[sub.pk])
        user = User.objects.create_user(username="plain", email="p@example.com", password="x", is_verified=True)
        self.client.force_login(user)
        self.assertNotEqual(self.client.get(url).status_code, 200)


class LoginRedirectTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username="u", email="u@example.com", password="pass12345", is_verified=True)

    def _login(self, next_url):
        return self.client.post(
            reverse("users:login") + f"?next={next_url}", {"username": "u", "password": "pass12345"},
        )

    def test_external_next_ignored(self):
        response = self._login("https://evil.example.com/")
        self.assertEqual(response.status_code, 302)
        self.assertFalse(response["Location"].startswith("https://evil"))

    def test_local_next_kept(self):
        response = self._login("/matches/")
        self.assertEqual(response["Location"], "/matches/")

    def test_logout_requires_post(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse("users:logout")).status_code, 405)
        self.client.post(reverse("users:logout"))
        self.assertNotIn("_auth_user_id", self.client.session)


class TwoFactorNextTests(TestCase):
    def test_backslash_trick_rejected(self):
        from dashboard.views_2fa import _safe_next

        request = RequestFactory().get("/", {"next": "/\\evil.example.com"})
        self.assertEqual(_safe_next(request, "/fallback/"), "/fallback/")


class VerificationTokenTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_login_after_expiry_issues_fresh_token(self):
        user = User.objects.create_user(username="late", email="late@example.com", password="pass12345")
        User.objects.filter(pk=user.pk).update(verification_token_created_at=timezone.now() - timedelta(days=5))
        old_token = user.verification_token
        with mock.patch("notifications.tasks.send_email_verification.delay"):
            self.client.post(reverse("users:login"), {"username": "late", "password": "pass12345"})
        user.refresh_from_db()
        self.assertNotEqual(user.verification_token, old_token)
        self.assertGreater(user.verification_token_created_at, timezone.now() - timedelta(minutes=1))
        response = self.client.get(reverse("users:verify_email", args=[user.verification_token]))
        user.refresh_from_db()
        self.assertTrue(user.is_verified)
        self.assertEqual(response.status_code, 302)


class DashboardAccessTests(TestCase):
    def test_staff_without_grant_denied(self):
        from dashboard.access import user_can_access_section

        staff = User.objects.create_user(username="s", email="s@example.com", password="x", is_staff=True)
        self.assertFalse(user_can_access_section(staff, "scripts"))
        StaffAccessGrant.objects.create(user=staff, allowed_sections=["scripts"])
        staff.refresh_from_db()
        self.assertTrue(user_can_access_section(staff, "scripts"))

    @override_settings(STAFF_2FA_ENFORCED=False)
    def test_staff_cannot_ban_superuser(self):
        staff = User.objects.create_user(username="s", email="s@example.com", password="x", is_staff=True)
        StaffAccessGrant.objects.create(user=staff, allowed_sections=["users"])
        boss = User.objects.create_superuser(username="boss", email="boss@example.com", password="x")
        self.client.force_login(staff)
        self.client.post(reverse("dashboard:user_toggle_ban", args=[boss.id]))
        boss.refresh_from_db()
        self.assertTrue(boss.is_active)


class SeedGuardTests(TestCase):
    @override_settings(ALLOW_SEED_COMMANDS=False)
    def test_seed_command_refused(self):
        with self.assertRaises(CommandError):
            call_command("seed_match_votes", "--status", "finished")

    @override_settings(ALLOW_SEED_COMMANDS=False)
    def test_seed_hidden_from_dashboard_registry(self):
        from dashboard.commands_registry import categories, get_command

        self.assertIsNone(get_command("seed_match_votes"))
        self.assertNotIn("seed", [key for key, _label, _specs in categories()])


class CanonicalEmailTests(TestCase):
    def test_gmail_aliases_collapse(self):
        self.assertEqual(canonical_email("Ivan.Petrov+dopx@GoogleMail.com"), "ivanpetrov@gmail.com")
        self.assertEqual(canonical_email("user+1@mail.ru"), "user@mail.ru")

    def test_profile_cannot_take_alias_of_existing_email(self):
        User.objects.create_user(username="a", email="ivan@gmail.com", password="x")
        other = User.objects.create_user(username="b", email="other@example.com", password="x")
        form = UserProfileForm(data={"email": "i.van+2@gmail.com", "city": "", "bio": ""}, instance=other)
        self.assertFalse(form.is_valid())
        self.assertIn("email", form.errors)


class XPRemainderTests(TestCase):
    def test_fractions_accumulate(self):
        user = User.objects.create_user(username="x", email="x@example.com", password="x")
        xp = UserXP.objects.create(user=user)
        for _ in range(3):
            xp.add_xp(0.45)
        xp.refresh_from_db()
        self.assertEqual(xp.total_xp, 1)
        self.assertAlmostEqual(xp.xp_remainder, 0.35, places=5)


class WizardAfterCompletionTests(TestCase):
    def setUp(self):
        league = League.objects.create(name="L", country="KZ")
        season = Season.objects.create(league=league, year="2026")
        self.match = Match.objects.create(
            league=league, season=season, home_team=Team.objects.create(name="H"),
            away_team=Team.objects.create(name="A"), status="finished",
            start_time=timezone.now() - timedelta(hours=3), voting_open_until=timezone.now() + timedelta(hours=40),
        )
        self.user = User.objects.create_user(username="w", email="w@example.com", password="x", is_verified=True)
        ContextEvaluation.objects.create(user=self.user, match=self.match)
        EvaluationSession.objects.create(
            user=self.user, match=self.match, status="completed", completed_at=timezone.now(),
            completed_steps=["context", "teams", "players", "coaches", "referee"],
        )
        self.client.force_login(self.user)

    def test_team_step_closed_after_completion(self):
        h = self.match.home_team_id
        data = {f"team_{h}_{k}": 10 for k in ("tactics", "effort", "organization", "mentality")}
        a = self.match.away_team_id
        data.update({f"team_{a}_{k}": 10 for k in ("tactics", "effort", "organization", "mentality")})
        response = self.client.post(reverse("evaluations:teams", args=[self.match.id]), data)
        self.assertEqual(response.status_code, 302)
        self.assertFalse(TeamEvaluation.objects.filter(user=self.user).exists())

    def test_unverified_email_cannot_vote(self):
        User.objects.filter(pk=self.user.pk).update(is_verified=False)
        EvaluationSession.objects.filter(user=self.user).delete()
        response = self.client.get(reverse("evaluations:context", args=[self.match.id]))
        self.assertRedirects(response, reverse("matches:detail", args=[self.match.id]), fetch_redirect_response=False)


class DashboardSessionDeleteTests(TestCase):
    @override_settings(STAFF_2FA_ENFORCED=False, CELERY_TASK_ALWAYS_EAGER=False)
    def test_ratings_recalculated_immediately(self):
        from unittest import mock

        from aggregates.models import MatchAggregate
        from evaluations.models import MatchEvaluation

        league = League.objects.create(name="L", country="KZ")
        season = Season.objects.create(league=league, year="2026")
        match = Match.objects.create(
            league=league, season=season, home_team=Team.objects.create(name="H"),
            away_team=Team.objects.create(name="A"), status="finished",
            start_time=timezone.now() - timedelta(days=10), voting_open_until=timezone.now() - timedelta(days=8),
        )
        voter = User.objects.create_user(username="v", email="v@example.com", password="x", is_verified=True)
        session = EvaluationSession.objects.create(user=voter, match=match, status="completed", completed_at=timezone.now())
        MatchEvaluation.objects.create(user=voter, match=match, entertainment=9, tension=9, fairness=9)
        MatchAggregate.objects.create(match=match, total_votes=1, drama_index=81, avg_entertainment=9)

        staff = User.objects.create_superuser(username="boss", email="boss@example.com", password="x")
        self.client.force_login(staff)
        # Очередь «потеряна» — пересчёт всё равно должен пройти синхронно.
        with mock.patch("celery.app.task.Task.apply_async"):
            self.client.post(reverse("dashboard:evaluation_session_delete", args=[session.id]))
        self.assertEqual(MatchAggregate.objects.get(match=match).total_votes, 0)
