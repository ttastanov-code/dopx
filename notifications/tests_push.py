# notifications/tests_push.py
"""Тесты доставки push: TTL/срочность, чистка мёртвых подписок, свежесть live-событий,
дедуп приглашения к прогнозу."""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from events.models import MatchEvent
from leagues.models import League
from matches.models import Match
from seasons.models import Season
from teams.models import Team
from users.models import Follow, PushSubscription

from .models import Notification
from .services import PUSH_HTTP_TIMEOUT, PUSH_PROFILES, send_push_to_users
from .tasks import notify_followers_match_event, notify_prediction_closing_soon

User = get_user_model()

VAPID = dict(VAPID_PUBLIC_KEY="pub", VAPID_PRIVATE_KEY="priv", VAPID_ADMIN_EMAIL="admin@test.local")


def _setup_match(status="live", start_delta=timedelta(minutes=-30)):
    league = League.objects.create(name="L", country="KZ")
    season = Season.objects.create(league=league, year="2026", is_active=True)
    home = Team.objects.create(name="Кайрат")
    away = Team.objects.create(name="Астана")
    start = timezone.now() + start_delta
    match = Match.objects.create(
        league=league, season=season, home_team=home, away_team=away,
        status=status, start_time=start, voting_open_until=start + timedelta(hours=48),
        home_score=1 if status == "live" else None, away_score=0 if status == "live" else None,
    )
    return match, home, away


def _user(name):
    return User.objects.create_user(username=name, email=f"{name}@example.com", password="x", is_verified=True)


def _sub(user, n):
    return PushSubscription.objects.create(
        user=user, endpoint=f"https://push.example.com/{user.username}/{n}", p256dh="k", auth="a",
    )


@override_settings(**VAPID)
class SendPushToUsersTests(TestCase):
    def setUp(self):
        self.u1, self.u2 = _user("a"), _user("b")
        _sub(self.u1, 1)
        _sub(self.u1, 2)
        _sub(self.u2, 1)

    @patch("pywebpush.webpush")
    def test_sends_with_ttl_urgency_and_timeout(self, mocked):
        sent = send_push_to_users([self.u1.id, self.u2.id], title="t", body="b", kind="match_event", tag="live-1")

        self.assertEqual(sent, 3)
        kwargs = mocked.call_args.kwargs
        self.assertEqual(kwargs["ttl"], PUSH_PROFILES["match_event"]["ttl"])
        self.assertEqual(kwargs["headers"]["Urgency"], "high")
        self.assertEqual(kwargs["timeout"], PUSH_HTTP_TIMEOUT)
        self.assertIn('"tag": "live-1"', kwargs["data"])

    @patch("pywebpush.webpush")
    def test_default_ttl_is_not_zero(self, mocked):
        send_push_to_users([self.u2.id], title="t", body="b")
        self.assertGreater(mocked.call_args.kwargs["ttl"], 0)

    @patch("pywebpush.webpush")
    def test_gone_subscription_is_deleted(self, mocked):
        from pywebpush import WebPushException

        gone = WebPushException("gone", response=MagicMock(status_code=410))
        mocked.side_effect = [gone, None, None]

        sent = send_push_to_users([self.u1.id, self.u2.id], title="t", body="b")

        self.assertEqual(sent, 2)
        self.assertEqual(PushSubscription.objects.count(), 2)

    @override_settings(VAPID_PRIVATE_KEY="")
    @patch("pywebpush.webpush")
    def test_no_vapid_keys_no_send(self, mocked):
        self.assertEqual(send_push_to_users([self.u1.id], title="t", body="b"), 0)
        mocked.assert_not_called()


class MatchEventPushFreshnessTests(TestCase):
    def setUp(self):
        self.match, self.home, _ = _setup_match()
        self.fan = _user("fan")
        Follow.objects.create(user=self.fan, team=self.home)
        self.event = MatchEvent.objects.create(match=self.match, minute=30, event_type="goal", team_side="home")

    @patch("notifications.services.send_push_to_users", return_value=1)
    def test_fresh_event_is_pushed_with_live_tag(self, mocked):
        notify_followers_match_event(str(self.match.id), str(self.event.id))

        mocked.assert_called_once()
        self.assertEqual(mocked.call_args.kwargs["kind"], "match_event")
        self.assertEqual(mocked.call_args.kwargs["tag"], f"live-{self.match.id}")
        self.assertTrue(Notification.objects.filter(user=self.fan, notification_type="match_event").exists())

    @patch("notifications.services.send_push_to_users", return_value=1)
    def test_stale_event_gets_inapp_only(self, mocked):
        MatchEvent.objects.filter(id=self.event.id).update(updated_at=timezone.now() - timedelta(minutes=30))

        notify_followers_match_event(str(self.match.id), str(self.event.id))

        mocked.assert_not_called()
        self.assertTrue(Notification.objects.filter(user=self.fan, notification_type="match_event").exists())


@override_settings(CELERY_TASK_ALWAYS_EAGER=False)
class PredictionClosingSoonTests(TestCase):
    def setUp(self):
        self.match, self.home, _ = _setup_match(status="scheduled", start_delta=timedelta(minutes=40))
        self.fan = _user("fan")
        self.other = _user("other")
        Follow.objects.create(user=self.fan, team=self.home)

    @patch("notifications.tasks._send_match_email_chunk.delay")
    @patch("notifications.services.send_push_to_users", return_value=1)
    def test_push_only_to_followers_and_no_duplicates(self, mocked_push, _mocked_email):
        notify_prediction_closing_soon()
        notify_prediction_closing_soon()  # второй тик через 30 минут

        self.assertEqual(mocked_push.call_count, 1)
        self.assertEqual(set(mocked_push.call_args.args[0]), {self.fan.id})
        # In-app — всем, но по одному разу.
        self.assertEqual(
            Notification.objects.filter(related_match=self.match, notification_type="prediction_closing").count(), 2,
        )


class PushDevicesEndpointTests(TestCase):
    def setUp(self):
        self.user = _user("dev")
        self.client.force_login(self.user)
        self.payload = {"endpoint": "https://fcm.googleapis.com/fcm/send/abc", "keys": {"p256dh": "k", "auth": "a"}}

    def _subscribe(self):
        import json

        return self.client.post("/users/push/subscribe/", data=json.dumps(self.payload), content_type="application/json").json()

    def test_subscribe_reports_created_once(self):
        self.assertTrue(self._subscribe()["created"])
        self.assertFalse(self._subscribe()["created"])
        self.assertEqual(PushSubscription.objects.filter(user=self.user).count(), 1)

    def test_devices_partial_lists_endpoint(self):
        self._subscribe()
        resp = self.client.get("/users/push/devices/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'data-push-endpoint="https://fcm.googleapis.com/fcm/send/abc"')
