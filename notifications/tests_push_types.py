# notifications/tests_push_types.py
"""Тесты новых типов push: настройки по типам, результат прогноза, незавершённая оценка,
бейджи, сборная тура, перенос/отмена матча."""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from evaluations.models import EvaluationSession
from matches.models import Match
from parsers.sportmonks.importers import _detect_match_change
from players.models import Player
from predictions.models import MatchPrediction
from round_squad.models import RoundBestXI, RoundBestXISlot
from round_squad.tasks import notify_round_xi_followers
from users.models import Follow

from .models import Notification
from .services import send_push_to_users
from .tasks import notify_followers_match_changed, notify_prediction_results, notify_unfinished_evaluations
from .tests_push import VAPID, _setup_match, _sub, _user

LOCMEM = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}


def _kinds(mocked) -> list[str]:
    return [c.kwargs["kind"] for c in mocked.call_args_list]


def _calls_of(mocked, kind: str) -> list:
    return [c for c in mocked.call_args_list if c.kwargs["kind"] == kind]


@override_settings(**VAPID)
class PushPreferenceTests(TestCase):
    def setUp(self):
        self.on, self.off = _user("on"), _user("off")
        _sub(self.on, 1)
        _sub(self.off, 1)
        self.off.notification_settings = {"push_live": False}
        self.off.save()

    @patch("pywebpush.webpush")
    def test_disabled_kind_is_filtered(self, mocked):
        sent = send_push_to_users([self.on.id, self.off.id], title="t", body="b", kind="match_event")
        self.assertEqual(sent, 1)

    @patch("pywebpush.webpush")
    def test_other_kinds_still_delivered(self, mocked):
        sent = send_push_to_users([self.on.id, self.off.id], title="t", body="b", kind="match_changed")
        self.assertEqual(sent, 2)

    @patch("pywebpush.webpush")
    def test_default_kind_ignores_settings(self, mocked):
        self.assertEqual(send_push_to_users([self.off.id], title="t", body="b"), 1)


@override_settings(CACHES=LOCMEM)
class PredictionResultPushTests(TestCase):
    def setUp(self):
        cache.clear()
        self.match, _, _ = _setup_match(status="finished", start_delta=timedelta(hours=-3))
        self.match.home_score, self.match.away_score = 2, 0
        self.match.end_time = timezone.now() - timedelta(hours=1)
        self.match.save()
        self.right, self.wrong = _user("right"), _user("wrong")
        self.right.prediction_streak = 2
        self.right.save()
        MatchPrediction.objects.create(match=self.match, user=self.right, choice="1")
        MatchPrediction.objects.create(match=self.match, user=self.wrong, choice="2")

    @patch("notifications.services.send_push_to_users", return_value=1)
    def test_push_with_streak_once(self, mocked):
        notify_prediction_results()
        notify_prediction_results()

        calls = _calls_of(mocked, "prediction_result")
        self.assertEqual(len(calls), 2)
        by_title = {c.kwargs["title"]: c for c in calls}
        self.assertIn("Серия: 3 подряд", by_title["✅ Прогноз сбылся!"].kwargs["body"])
        self.assertEqual(by_title["❌ Прогноз не сбылся"].args[0], [self.wrong.id])

    @patch("notifications.services.send_push_to_users", return_value=1)
    def test_catchup_of_old_match_is_silent(self, mocked):
        Match.objects.filter(id=self.match.id).update(end_time=timezone.now() - timedelta(days=2))
        notify_prediction_results()

        self.assertEqual(_calls_of(mocked, "prediction_result"), [])
        self.assertTrue(Notification.objects.filter(notification_type="prediction_result").exists())


class UnfinishedEvaluationReminderTests(TestCase):
    def setUp(self):
        self.match, _, _ = _setup_match(status="finished", start_delta=timedelta(hours=-46))
        Match.objects.filter(id=self.match.id).update(voting_open_until=timezone.now() + timedelta(minutes=90))
        self.quitter, self.done = _user("quitter"), _user("done")
        EvaluationSession.objects.create(user=self.quitter, match=self.match, status="in_progress")
        EvaluationSession.objects.create(user=self.done, match=self.match, status="completed", completed_at=timezone.now())

    @patch("notifications.services.send_push_to_users", return_value=1)
    def test_reminds_only_unfinished_once(self, mocked):
        notify_unfinished_evaluations()
        notify_unfinished_evaluations()

        self.assertEqual(mocked.call_count, 1)
        self.assertEqual(set(mocked.call_args.args[0]), {self.quitter.id})
        self.assertEqual(mocked.call_args.kwargs["kind"], "evaluation_reminder")

    @patch("notifications.services.send_push_to_users", return_value=1)
    def test_far_from_closing_is_skipped(self, mocked):
        Match.objects.filter(id=self.match.id).update(voting_open_until=timezone.now() + timedelta(hours=10))
        notify_unfinished_evaluations()
        mocked.assert_not_called()


class BadgePushTests(TestCase):
    @patch("notifications.services.send_push_to_users", return_value=1)
    @patch("users.services.check_and_award_badges")
    def test_new_badge_is_pushed(self, mocked_award, mocked_push):
        from users.tasks import check_and_award_badges_task

        user = _user("badger")
        badge = SimpleNamespace(badge_type="first", get_badge_type_display=lambda: "Первая оценка")
        mocked_award.return_value = [badge]

        check_and_award_badges_task(str(user.id))

        self.assertEqual(_kinds(mocked_push), ["achievement"])
        self.assertEqual(mocked_push.call_args.kwargs["body"], "Первая оценка")


class RoundXIFollowersTests(TestCase):
    def setUp(self):
        self.match, self.home, _ = _setup_match(status="finished", start_delta=timedelta(days=-3))
        self.player = Player.objects.create(first_name="Дастан", last_name="Сатпаев", team=self.home)
        self.round_xi = RoundBestXI.objects.create(season=self.match.season, tour=1, is_final=True)
        RoundBestXISlot.objects.create(
            round_best_xi=self.round_xi, slot_code="ST", order=1,
            content_type=ContentType.objects.get_for_model(Player), object_id=self.player.id,
        )
        self.player_fan, self.team_fan, self.nobody = _user("pf"), _user("tf"), _user("nb")
        Follow.objects.create(user=self.player_fan, player=self.player)
        Follow.objects.create(user=self.team_fan, team=self.home)

    @patch("notifications.services.send_push_to_users", return_value=1)
    def test_followers_notified_once(self, mocked):
        self.assertEqual(notify_round_xi_followers(self.round_xi), 2)
        self.assertEqual(notify_round_xi_followers(self.round_xi), 0)

        recipients = {uid for c in mocked.call_args_list for uid in c.args[0]}
        self.assertEqual(recipients, {self.player_fan.id, self.team_fan.id})
        self.assertIn("Дастан Сатпаев", mocked.call_args.kwargs["title"])
        self.assertFalse(Notification.objects.filter(user=self.nobody).exists())


class MatchChangeDetectionTests(TestCase):
    def _m(self, status, start):
        return SimpleNamespace(status=status, start_time=start)

    def test_postponed_and_cancelled(self):
        start = timezone.now() + timedelta(days=1)
        self.assertEqual(_detect_match_change(self._m("scheduled", start), self._m("postponed", start)), "postponed")
        self.assertEqual(_detect_match_change(self._m("scheduled", start), self._m("cancelled", start)), "cancelled")

    def test_kickoff_shift(self):
        start = timezone.now() + timedelta(days=2)
        before = self._m("scheduled", start)
        self.assertIsNone(_detect_match_change(before, self._m("scheduled", start + timedelta(minutes=30))))
        self.assertEqual(_detect_match_change(before, self._m("scheduled", start + timedelta(hours=2))), "rescheduled")

    def test_far_future_shift_is_ignored(self):
        start = timezone.now() + timedelta(days=60)
        self.assertIsNone(_detect_match_change(self._m("scheduled", start), self._m("scheduled", start + timedelta(days=1))))

    def test_postponed_gets_new_date(self):
        start = timezone.now() + timedelta(days=5)
        self.assertEqual(_detect_match_change(self._m("postponed", start), self._m("scheduled", start)), "rescheduled")


class MatchChangedTaskTests(TestCase):
    def setUp(self):
        self.match, self.home, _ = _setup_match(status="postponed", start_delta=timedelta(days=1))
        self.fan = _user("fan")
        Follow.objects.create(user=self.fan, team=self.home)

    @patch("notifications.services.send_push_to_users", return_value=1)
    def test_notifies_followers_once(self, mocked):
        notify_followers_match_changed(str(self.match.id), "postponed")
        notify_followers_match_changed(str(self.match.id), "postponed")

        self.assertEqual(mocked.call_count, 1)
        self.assertEqual(mocked.call_args.kwargs["kind"], "match_changed")
        self.assertEqual(Notification.objects.filter(user=self.fan, notification_type="match_changed").count(), 1)
