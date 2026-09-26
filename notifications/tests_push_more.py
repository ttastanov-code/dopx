# notifications/tests_push_more.py
"""Дедуп матчевых пушей, закрытие голосования, рейтинги открыты, ответ на обращение."""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from aggregates.models import PlayerMatchAggregate
from evaluations.models import EvaluationSession
from matches.models import Match
from players.models import Player
from users.models import Follow

from .models import ContactSubmission, Notification
from .tasks import (
    notify_contact_resolved,
    notify_followers_match_started,
    notify_ratings_published,
    notify_voting_closing_soon,
)
from .tests_push import _setup_match, _user

LOCMEM = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}


@override_settings(CACHES=LOCMEM)
class MatchPushDedupTests(TestCase):
    @patch("notifications.services.send_push_to_users", return_value=1)
    def test_match_started_sent_once(self, mocked):
        match, home, _ = _setup_match()
        Follow.objects.create(user=_user("fan"), team=home)
        notify_followers_match_started(str(match.id))
        notify_followers_match_started(str(match.id))
        self.assertEqual(mocked.call_count, 1)
        self.assertEqual(Notification.objects.filter(notification_type="match_started").count(), 1)


@override_settings(CACHES=LOCMEM)
class VotingClosingTests(TestCase):
    def setUp(self):
        cache.clear()
        self.match, home, _ = _setup_match(status="finished", start_delta=timedelta(hours=-3))
        Match.objects.filter(id=self.match.id).update(voting_open_until=timezone.now() + timedelta(minutes=40))
        self.rated, self.started, self.idle = _user("rated"), _user("started"), _user("idle")
        for u in (self.rated, self.started, self.idle):
            Follow.objects.create(user=u, team=home)
        EvaluationSession.objects.create(user=self.rated, match=self.match, status="completed", completed_at=timezone.now())
        EvaluationSession.objects.create(user=self.started, match=self.match, status="in_progress")

    @patch("notifications.tasks._send_match_email_chunk.delay")
    @patch("notifications.services.send_push_to_users", return_value=1)
    def test_skips_rated_and_pushes_only_idle_once(self, push, _email):
        notify_voting_closing_soon()
        notify_voting_closing_soon()
        recipients = set(Notification.objects.filter(notification_type="voting_closing").values_list("user_id", flat=True))
        self.assertNotIn(self.rated.id, recipients)
        self.assertEqual(push.call_count, 1)
        self.assertEqual({str(u) for u in push.call_args.args[0]}, {str(self.idle.id)})
        self.assertEqual(push.call_args.kwargs["kind"], "voting_closing")


@override_settings(CACHES=LOCMEM)
class RatingsPublishedTests(TestCase):
    def setUp(self):
        cache.clear()
        self.match, home, _ = _setup_match(status="finished", start_delta=timedelta(hours=-3))
        self.match.home_score, self.match.away_score = 2, 1
        self.match.voting_open_until = timezone.now() - timedelta(minutes=30)
        self.match.save()
        self.rater = _user("rater")
        EvaluationSession.objects.create(user=self.rater, match=self.match, status="completed", completed_at=timezone.now())
        player = Player.objects.create(first_name="Иван", last_name="Лучший", team=home)
        PlayerMatchAggregate.objects.create(player=player, match=self.match, performance_score=8.7, total_votes=50)

    @patch("notifications.services.send_push_to_users", return_value=1)
    def test_notifies_raters_with_mvp_once(self, push):
        notify_ratings_published()
        notify_ratings_published()
        note = Notification.objects.get(notification_type="ratings_published")
        self.assertEqual(note.user_id, self.rater.id)
        self.assertIn("Иван Лучший (8.7)", note.message)
        self.assertEqual(push.call_count, 1)

    @patch("notifications.services.send_push_to_users", return_value=1)
    def test_too_early_after_close_is_skipped(self, push):
        Match.objects.filter(id=self.match.id).update(voting_open_until=timezone.now() - timedelta(minutes=5))
        notify_ratings_published()
        push.assert_not_called()


class ContactResolvedTests(TestCase):
    @patch("notifications.tasks.send_push_task.delay")
    def test_user_gets_notification_guest_does_not(self, push):
        user = _user("author")
        ticket = ContactSubmission.objects.create(user=user, subject="Ошибка счёта", message="m", category="data_error", status="resolved")
        notify_contact_resolved(ticket)
        guest = ContactSubmission.objects.create(guest_email="g@example.com", subject="s", message="m", status="resolved")
        notify_contact_resolved(guest)
        note = Notification.objects.get(notification_type="contact_reply")
        self.assertEqual(note.user_id, user.id)
        self.assertIn("исправлены", note.title)
        push.assert_called_once()
