# notifications/tests.py
"""Тесты notifications: рассылка подписчикам (email+push+in-app), дедуп, Redis-lock.
Задачи вызываются напрямую. EMAIL_HOST_USER задан, иначе письма не попадут в outbox.
"""
from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from leagues.models import League
from lineups.models import MatchLineup, MatchLineupPlayer
from matches.models import Match
from players.models import Player
from predictions.models import MatchPrediction
from seasons.models import Season
from teams.models import Team
from users.models import Follow

from .models import Notification
from .tasks import (
    notify_followers_lineups_available,
    notify_followers_match_activity,
    notify_followers_match_started,
    notify_prediction_results,
    notify_voting_closing_soon,
    send_notification_digest,
)

User = get_user_model()

# LocMemCache для cache.add()-локов — без Redis.
LOCMEM_CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "test-notifications-lock",
    }
}

# Без EMAIL_HOST_USER _send_email_to_user не пишет в outbox.
EMAIL_TEST_SETTINGS = dict(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    EMAIL_HOST_USER="test-smtp-user",
    DEFAULT_FROM_EMAIL="noreply@dopx.kz",
)


def _make_league_season_teams():
    league = League.objects.create(name="Test League", country="KZ")
    season = Season.objects.create(league=league, year="2026")
    home = Team.objects.create(name="Kairat")
    away = Team.objects.create(name="Astana")
    return league, season, home, away


@override_settings(**EMAIL_TEST_SETTINGS)
class NotifyFollowersMatchActivityTests(TestCase):
    """notify_followers_match_activity: адресная рассылка подписчикам и предсказавшим."""

    def setUp(self):
        self.league, self.season, self.home, self.away = _make_league_season_teams()
        now = timezone.now()
        self.match = Match.objects.create(
            league=self.league,
            season=self.season,
            home_team=self.home,
            away_team=self.away,
            start_time=now - timedelta(hours=2),
            end_time=now,
            status="finished",
            home_score=2,
            away_score=1,
            voting_open_until=now + timedelta(hours=48),
        )
        self.follower = User.objects.create_user(
            username="follower", email="follower@example.com", password="pass12345",
            is_verified=True,
        )
        Follow.objects.create(user=self.follower, team=self.home)

        self.non_follower = User.objects.create_user(
            username="stranger", email="stranger@example.com", password="pass12345",
            is_verified=True,
        )

    def test_follower_gets_email_and_inapp_notification_on_voting_open(self):
        """Подписчик получает email при открытии голосования."""
        result = notify_followers_match_activity(str(self.match.id))

        self.assertEqual(result["notified"], 1)
        self.assertEqual(result["emailed"], 1)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(self.follower.email, mail.outbox[0].to)

        notif = Notification.objects.get(user=self.follower, related_match=self.match)
        self.assertEqual(notif.notification_type, "voting_open")

    def test_non_follower_receives_nothing(self):
        """Неподписанный ничего не получает."""
        notify_followers_match_activity(str(self.match.id))

        self.assertFalse(Notification.objects.filter(user=self.non_follower).exists())
        self.assertEqual(len(mail.outbox), 1)  # только письмо подписчику
        self.assertNotIn(self.non_follower.email, mail.outbox[0].to)

    def test_follower_of_away_team_is_also_notified(self):
        """Подписка на любую из двух команд."""
        away_follower = User.objects.create_user(
            username="away_fan", email="away_fan@example.com", password="pass12345", is_verified=True,
        )
        Follow.objects.create(user=away_follower, team=self.away)

        result = notify_followers_match_activity(str(self.match.id))

        self.assertEqual(result["notified"], 2)  # подписчик хозяев + подписчик гостей
        self.assertTrue(Notification.objects.filter(user=away_follower).exists())

    def test_follower_of_player_in_lineup_is_notified(self):
        """Подписка на игрока — через состав матча."""
        player = Player.objects.create(first_name="Test", last_name="Player", team=self.home)
        lineup = MatchLineup.objects.create(match=self.match, team=self.home, side="home")
        MatchLineupPlayer.objects.create(lineup=lineup, player=player, is_starting=True)

        player_follower = User.objects.create_user(
            username="player_fan", email="player_fan@example.com", password="pass12345", is_verified=True,
        )
        Follow.objects.create(user=player_follower, player=player)

        result = notify_followers_match_activity(str(self.match.id))

        self.assertTrue(Notification.objects.filter(user=player_follower).exists())
        # подписчик команды + подписчик игрока, без дублей
        self.assertEqual(result["notified"], 2)

    def test_unverified_follower_gets_inapp_but_no_email(self):
        """Неверифицированный не получает email, но in-app получает."""
        unverified = User.objects.create_user(
            username="unverified", email="unverified@example.com", password="pass12345",
            is_verified=False,
        )
        Follow.objects.create(user=unverified, team=self.home)

        notify_followers_match_activity(str(self.match.id))

        self.assertTrue(Notification.objects.filter(user=unverified).exists())
        all_recipients = [addr for msg in mail.outbox for addr in msg.to]
        self.assertNotIn(unverified.email, all_recipients)

    def test_bot_pool_follower_gets_inapp_but_no_email(self):
        """Боты (test_user_bot_*@test.dopx.local) не получают email, in-app — да."""
        bot_follower = User.objects.create_user(
            username="test_user_bot_0001", email="test_user_bot_0001@test.dopx.local",
            password="pass12345", is_verified=True,
        )
        Follow.objects.create(user=bot_follower, team=self.home)

        notify_followers_match_activity(str(self.match.id))

        self.assertTrue(Notification.objects.filter(user=bot_follower).exists())
        all_recipients = [addr for msg in mail.outbox for addr in msg.to]
        self.assertNotIn(bot_follower.email, all_recipients)

    def test_follower_with_email_channel_disabled_gets_inapp_but_no_email(self):
        """email_match_finished=False отключает только email."""
        opted_out = User.objects.create_user(
            username="opted_out", email="opted_out@example.com", password="pass12345",
            is_verified=True,
        )
        opted_out.notification_settings = {"email_match_finished": False}
        opted_out.save()
        Follow.objects.create(user=opted_out, team=self.home)

        notify_followers_match_activity(str(self.match.id))

        self.assertTrue(Notification.objects.filter(user=opted_out).exists())
        all_recipients = [addr for msg in mail.outbox for addr in msg.to]
        self.assertNotIn(opted_out.email, all_recipients)

    def test_push_is_attempted_for_every_follower_best_effort(self):
        """Push вызывается для каждого подписчика."""
        from unittest.mock import patch

        with patch("notifications.services.send_push_to_users") as mocked_push:
            mocked_push.return_value = 0
            result = notify_followers_match_activity(str(self.match.id))

        self.assertEqual(mocked_push.call_count, 1)
        self.assertEqual(set(mocked_push.call_args.args[0]), {self.follower.id})  # один подписчик
        self.assertEqual(mocked_push.call_args.kwargs["kind"], "match_finished")
        self.assertEqual(mocked_push.call_args.kwargs["tag"], f"live-{self.match.id}")
        self.assertEqual(result["notified"], 1)

    def test_no_match_found_returns_zero_without_error(self):
        """Нет матча — no-op."""
        import uuid

        result = notify_followers_match_activity(str(uuid.uuid4()))
        self.assertEqual(result, {"notified": 0})
        self.assertEqual(len(mail.outbox), 0)

    def test_predictor_without_follow_is_also_notified(self):
        """Предсказавший без подписки тоже в аудитории."""
        predictor = User.objects.create_user(
            username="predictor", email="predictor@example.com", password="pass12345",
            is_verified=True,
        )
        MatchPrediction.objects.create(
            match=self.match, user=predictor, choice=MatchPrediction.CHOICE_HOME,
        )

        result = notify_followers_match_activity(str(self.match.id))

        self.assertTrue(Notification.objects.filter(user=predictor, related_match=self.match).exists())
        self.assertEqual(result["notified"], 2)  # подписчик + предсказавший, без дублей
        all_recipients = [addr for msg in mail.outbox for addr in msg.to]
        self.assertIn(predictor.email, all_recipients)

    def test_follower_who_also_predicted_is_not_double_counted(self):
        """Подписка + прогноз от одного пользователя — одно уведомление."""
        MatchPrediction.objects.create(
            match=self.match, user=self.follower, choice=MatchPrediction.CHOICE_HOME,
        )

        result = notify_followers_match_activity(str(self.match.id))

        self.assertEqual(result["notified"], 1)
        self.assertEqual(Notification.objects.filter(related_match=self.match).count(), 1)


@override_settings(**EMAIL_TEST_SETTINGS)
class NotifyFollowersMatchStartedAndLineupsAvailableTests(TestCase):
    """Пуши «матч начался» / «составы» — та же аудитория, без email."""

    def setUp(self):
        self.league, self.season, self.home, self.away = _make_league_season_teams()
        now = timezone.now()
        self.match = Match.objects.create(
            league=self.league, season=self.season, home_team=self.home, away_team=self.away,
            start_time=now, status="live", voting_open_until=now + timedelta(hours=50),
        )
        self.follower = User.objects.create_user(
            username="follower", email="follower@example.com", password="pass12345", is_verified=True,
        )
        Follow.objects.create(user=self.follower, team=self.home)
        self.non_follower = User.objects.create_user(
            username="stranger", email="stranger@example.com", password="pass12345", is_verified=True,
        )

    def test_started_creates_inapp_for_follower_only(self):
        result = notify_followers_match_started(str(self.match.id))

        self.assertEqual(result["notified"], 1)
        notif = Notification.objects.get(user=self.follower, related_match=self.match)
        self.assertEqual(notif.notification_type, "match_started")
        self.assertFalse(Notification.objects.filter(user=self.non_follower).exists())
        # Email не шлём.
        self.assertEqual(len(mail.outbox), 0)

    def test_started_notifies_predictor_without_follow_too(self):
        predictor = User.objects.create_user(
            username="predictor", email="predictor@example.com", password="pass12345", is_verified=True,
        )
        MatchPrediction.objects.create(match=self.match, user=predictor, choice=MatchPrediction.CHOICE_HOME)

        result = notify_followers_match_started(str(self.match.id))

        self.assertEqual(result["notified"], 2)  # подписчик + предсказавший
        self.assertTrue(Notification.objects.filter(user=predictor, related_match=self.match).exists())

    def test_started_push_is_attempted_best_effort(self):
        from unittest.mock import patch

        with patch("notifications.services.send_push_to_users") as mocked_push:
            mocked_push.return_value = 0
            notify_followers_match_started(str(self.match.id))

        self.assertEqual(mocked_push.call_count, 1)
        self.assertEqual(set(mocked_push.call_args.args[0]), {self.follower.id})
        self.assertEqual(mocked_push.call_args.kwargs["kind"], "match_started")

    def test_lineups_available_creates_inapp_for_follower_only(self):
        result = notify_followers_lineups_available(str(self.match.id))

        self.assertEqual(result["notified"], 1)
        notif = Notification.objects.get(user=self.follower, related_match=self.match)
        self.assertEqual(notif.notification_type, "lineups_available")
        self.assertEqual(len(mail.outbox), 0)

    def test_no_match_found_returns_zero_without_error(self):
        import uuid

        self.assertEqual(notify_followers_match_started(str(uuid.uuid4())), {"notified": 0})
        self.assertEqual(notify_followers_lineups_available(str(uuid.uuid4())), {"notified": 0})


@override_settings(**EMAIL_TEST_SETTINGS, CACHES=LOCMEM_CACHES,
                    CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
class NotifyVotingBothDirectionsRegressionTests(TestCase):
    """Письмо уходит и при открытии, и при закрытии голосования."""

    def setUp(self):
        cache.clear()
        self.league, self.season, self.home, self.away = _make_league_season_teams()
        self.user = User.objects.create_user(
            username="fan", email="fan@example.com", password="pass12345", is_verified=True,
        )

    def test_email_sent_when_voting_opens(self):
        now = timezone.now()
        match = Match.objects.create(
            league=self.league, season=self.season, home_team=self.home, away_team=self.away,
            start_time=now - timedelta(hours=2), end_time=now, status="finished",
            home_score=1, away_score=0, voting_open_until=now + timedelta(hours=48),
        )
        Follow.objects.create(user=self.user, team=self.home)

        notify_followers_match_activity(str(match.id))

        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(self.user.email, mail.outbox[0].to)

    def test_email_sent_when_voting_closing_soon(self):
        now = timezone.now()
        match = Match.objects.create(
            league=self.league, season=self.season, home_team=self.home, away_team=self.away,
            start_time=now - timedelta(hours=50), end_time=now - timedelta(hours=48), status="finished",
            home_score=1, away_score=0, voting_open_until=now + timedelta(minutes=30),
        )
        # notify_voting_closing_soon — всем верифицированным, подписка не нужна.

        notify_voting_closing_soon()

        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(self.user.email, mail.outbox[0].to)
        self.assertTrue(
            Notification.objects.filter(
                user=self.user, notification_type="voting_closing", related_match=match,
            ).exists()
        )


@override_settings(**EMAIL_TEST_SETTINGS, CACHES=LOCMEM_CACHES,
                    CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
class NotifyVotingClosingSoonDedupTests(TestCase):
    """Задача каждые 30 минут, окно 1 час — дедуп по Notification('voting_closing')."""

    def setUp(self):
        cache.clear()
        self.league, self.season, self.home, self.away = _make_league_season_teams()
        now = timezone.now()
        self.match = Match.objects.create(
            league=self.league, season=self.season, home_team=self.home, away_team=self.away,
            start_time=now - timedelta(hours=50), end_time=now - timedelta(hours=48), status="finished",
            home_score=1, away_score=0, voting_open_until=now + timedelta(minutes=30),
        )
        self.user = User.objects.create_user(
            username="fan", email="fan@example.com", password="pass12345", is_verified=True,
        )

    def test_second_run_on_same_match_does_not_duplicate(self):
        first_result = notify_voting_closing_soon()
        self.assertEqual(first_result["matches_processed"], 1)
        first_notification_count = Notification.objects.filter(notification_type="voting_closing").count()
        first_email_count = len(mail.outbox)
        self.assertGreater(first_notification_count, 0)
        self.assertGreater(first_email_count, 0)

        second_result = notify_voting_closing_soon()

        self.assertEqual(second_result["matches_processed"], 0)
        self.assertEqual(second_result["skipped_already_notified"], 1)
        self.assertEqual(
            Notification.objects.filter(notification_type="voting_closing").count(),
            first_notification_count,
            "повторный прогон не должен создавать дубликаты Notification",
        )
        self.assertEqual(
            len(mail.outbox), first_email_count,
            "повторный прогон не должен слать повторные письма по уже обработанному матчу",
        )


@override_settings(**EMAIL_TEST_SETTINGS, CACHES=LOCMEM_CACHES)
class SendNotificationDigestTests(TestCase):
    """send_notification_digest: сводка раз в час для email_digest_mode=True + Redis-lock."""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username="digest_user", email="digest@example.com", password="pass12345",
            is_verified=True,
        )  # email_digest_mode=True по умолчанию

    def test_pending_notifications_are_collected_into_one_email(self):
        n1 = Notification.objects.create(
            user=self.user, notification_type="new_badge", title="Бейдж 1", message="msg1",
        )
        n2 = Notification.objects.create(
            user=self.user, notification_type="level_up", title="Уровень 2", message="msg2",
        )

        result = send_notification_digest()

        self.assertEqual(result["users_notified"], 1)
        self.assertEqual(result["notifications_sent"], 2)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(self.user.email, mail.outbox[0].to)

        n1.refresh_from_db()
        n2.refresh_from_db()
        self.assertIsNotNone(n1.email_sent_at)
        self.assertIsNotNone(n2.email_sent_at)

    def test_user_with_digest_mode_disabled_is_skipped(self):
        """Без дайджест-режима сводка не шлётся."""
        self.user.notification_settings = {"email_digest_mode": False}
        self.user.save()
        Notification.objects.create(
            user=self.user, notification_type="new_badge", title="Бейдж 1", message="msg1",
        )

        result = send_notification_digest()

        self.assertEqual(result["users_notified"], 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_already_sent_notifications_are_not_included(self):
        """Уже отправленное в сводку не попадает."""
        Notification.objects.create(
            user=self.user, notification_type="new_badge", title="Старый бейдж", message="msg",
            email_sent_at=timezone.now(),
        )

        result = send_notification_digest()

        self.assertEqual(result, {"users_notified": 0, "notifications_sent": 0})
        self.assertEqual(len(mail.outbox), 0)

    def test_lock_makes_concurrent_run_a_no_op(self):
        """Лок уже взят «другим воркером» — задача ничего не делает."""
        Notification.objects.create(
            user=self.user, notification_type="new_badge", title="Бейдж 1", message="msg1",
        )
        cache.add("notifications:lock:send_notification_digest", "1", timeout=600)

        result = send_notification_digest()

        self.assertEqual(result, {"users_notified": 0, "notifications_sent": 0, "skipped_locked": True})
        self.assertEqual(len(mail.outbox), 0)

    def test_lock_is_released_after_run_allowing_next_tick(self):
        """Лок снимается в finally — следующий прогон отрабатывает."""
        Notification.objects.create(
            user=self.user, notification_type="new_badge", title="Бейдж 1", message="msg1",
        )
        send_notification_digest()
        self.assertEqual(len(mail.outbox), 1)

        Notification.objects.create(
            user=self.user, notification_type="new_badge", title="Бейдж 2", message="msg2",
        )
        second_result = send_notification_digest()

        self.assertNotIn("skipped_locked", second_result)
        self.assertEqual(len(mail.outbox), 2)


@override_settings(**EMAIL_TEST_SETTINGS, CACHES=LOCMEM_CACHES)
class NotifyPredictionResultsLockTests(TestCase):
    """notify_prediction_results: такой же lock от параллельных прогонов."""

    def setUp(self):
        cache.clear()
        self.league, self.season, self.home, self.away = _make_league_season_teams()
        now = timezone.now()
        self.match = Match.objects.create(
            league=self.league, season=self.season, home_team=self.home, away_team=self.away,
            start_time=now - timedelta(hours=2), end_time=now - timedelta(hours=1), status="finished",
            home_score=2, away_score=0, voting_open_until=now + timedelta(hours=48),
        )
        self.user = User.objects.create_user(
            username="predictor", email="predictor@example.com", password="pass12345", is_verified=True,
        )
        MatchPrediction.objects.create(match=self.match, user=self.user, choice="1")

    def test_lock_prevents_duplicate_email_on_concurrent_run(self):
        cache.add("notifications:lock:notify_prediction_results", "1", timeout=600)

        result = notify_prediction_results()

        self.assertEqual(result, {"notified": 0, "skipped_locked": True})
        self.assertEqual(len(mail.outbox), 0)
        self.assertFalse(Notification.objects.filter(notification_type="prediction_result").exists())

    def test_runs_normally_and_dedupes_on_second_call(self):
        first_result = notify_prediction_results()
        self.assertEqual(first_result["notified"], 1)
        self.assertEqual(len(mail.outbox), 1)

        second_result = notify_prediction_results()

        self.assertEqual(second_result["notified"], 0)
        self.assertEqual(len(mail.outbox), 1)  # повторно не отправлено
        self.assertEqual(
            Notification.objects.filter(notification_type="prediction_result").count(), 1,
        )
