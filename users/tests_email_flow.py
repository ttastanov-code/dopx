# users/tests_email_flow.py
"""Почта: смена только через подтверждение, старые адреса не переиспользуются, оценки и прогнозы — с заполненным профилем."""
from datetime import timedelta
from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from users import emails
from users.models import UsedEmail, User


def verified_user(username="fan", email="fan@example.com", city="Алматы"):
    user = User.objects.create_user(username=username, email=email, password="Secret-pass-1", is_verified=True, city=city)
    emails.mark_confirmed(user)
    return user


@override_settings(STAFF_2FA_ENFORCED=False)
class EmailChangeTests(TestCase):
    def test_change_waits_for_confirmation_and_old_email_stays_reserved(self):
        user = verified_user()
        self.client.force_login(user)
        with mock.patch("notifications.tasks.send_email_change_confirmation.delay") as mail, self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("users:profile_edit"), {"email": "new@example.com", "city": "Алматы", "bio": "",
                                                             "is_profile_public": "on"})
        mail.assert_called_once()
        user.refresh_from_db()
        # Пока не подтвердил — старая почта работает, вход не заблокирован.
        self.assertEqual((user.email, user.pending_email, user.is_verified), ("fan@example.com", "new@example.com", True))

        self.client.get(reverse("users:confirm_email_change", args=[user.verification_token]))
        user.refresh_from_db()
        self.assertEqual((user.email, user.pending_email), ("new@example.com", ""))
        self.assertIsNotNone(user.email_verified_at)
        # Ссылка одноразовая.
        self.assertEqual(emails.confirm_change(user.verification_token)[0], None)

        # Старый адрес нельзя взять под новый аккаунт — и с «+меткой» тоже.
        self.assertTrue(emails.email_taken("fan@example.com"))
        self.assertTrue(emails.email_taken("Fan+x@example.com"))
        self.assertTrue(UsedEmail.objects.filter(user=user).count() == 2)

    def test_expired_and_taken_links(self):
        user = verified_user()
        other = verified_user("other", "other@example.com")
        emails.request_change(user, "free@example.com")
        User.objects.filter(pk=user.pk).update(verification_token_created_at=timezone.now() - timedelta(hours=49))
        self.assertIn("устарела", emails.confirm_change(user.verification_token)[1])
        emails.request_change(user, "other2@example.com")
        UsedEmail.objects.create(user=other, email_canonical="other2@example.com")
        self.assertIn("другому аккаунту", emails.confirm_change(user.verification_token)[1])

    def test_registration_rejects_previously_used_email(self):
        from users.forms import UserRegistrationForm

        user = verified_user()
        UsedEmail.objects.create(user=user, email_canonical="old@example.com")
        form = UserRegistrationForm(data={"email": "old@example.com"})
        form.is_valid()
        self.assertIn("email", form.errors)


@override_settings(PROFILE_REQUIRED=True, STAFF_2FA_ENFORCED=False)
class ProfileGateTests(TestCase):
    def setUp(self):
        from leagues.models import League
        from matches.models import Match
        from seasons.models import Season
        from teams.models import Team

        league = League.objects.create(name="КПЛ", country="KZ")
        season = Season.objects.create(league=league, year="2026", is_active=True)
        self.match = Match.objects.create(league=league, season=season, home_team=Team.objects.create(name="A"),
                                          away_team=Team.objects.create(name="B"), status="finished",
                                          home_score=1, away_score=0, start_time=timezone.now() - timedelta(hours=3),
                                          voting_open_until=timezone.now() + timedelta(hours=40))

    def test_incomplete_profile_is_sent_to_form(self):
        user = User.objects.create_user(username="nocity", email="n@example.com", password="x", is_verified=True)
        self.client.force_login(user)
        response = self.client.get(reverse("evaluations:context", args=[self.match.pk]))
        self.assertTrue(response.url.startswith(reverse("users:complete_profile")))
        widget = self.client.post(reverse("predictions:predict", args=[self.match.pk]), {"choice": "1"})
        self.assertContains(widget, "Заполнить профиль")
        page = self.client.get(reverse("users:profile"))
        self.assertContains(page, "Заполните профиль, чтобы оценивать")

    def test_complete_profile_passes(self):
        self.client.force_login(verified_user())
        response = self.client.get(reverse("evaluations:context", args=[self.match.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(self.client.get(reverse("users:profile")), "Заполните профиль, чтобы оценивать")
