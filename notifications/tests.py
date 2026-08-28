# notifications/tests.py
"""
notifications/ был единственным приложением в проекте без единого теста,
хотя именно в notifications/tasks.py недавно чинили баг "не приходит email
при открытии голосования на команду, на которую подписан пользователь"
(см. докстринг notify_followers_match_activity, раздел "ИСПРАВЛЕНО"). Ниже —
регрессионный набор на самые уязвимые с точки зрения тихого молчания места:
адресную рассылку подписчикам (email+push+in-app), дедупликацию
периодических задач по Notification и Redis-lock (cache.add()) от гонки
двух параллельных прогонов одной и той же periodic-задачи.

Все Celery-таски вызываются НАПРЯМУЮ (не через .delay/.apply_async) — тот же
паттерн, что в aggregates/tests.py: @shared_task(bind=True) оборачивает
функцию в Task.run, и вызов task(...) без явного self работает точно так же,
как в проде. CELERY_TASK_ALWAYS_EAGER нужен только там, где сама задача
внутри себя ставит в очередь под-задачи через .delay() (fan-out на чанки —
_send_match_email_chunk в notify_voting_closing_soon); notify_followers_
match_activity рассылает email синхронным циклом внутри себя и eager-режима
не требует.

EMAIL_HOST_USER явно переопределён в тестах, которые проверяют реальную
отправку письма: `_send_email_to_user` (notifications/tasks.py) считает
почтовый бэкенд "консольным dev-режимом" и просто логирует, БЕЗ реального
вызова email.send(), если `not settings.EMAIL_HOST_USER` — в проде это
настраивается переменной окружения, но в тестовом окружении её обычно нет,
и тогда `django.core.mail.outbox` остался бы пустым независимо от того,
работает ли рассылка правильно (тест бы молча "зеленел", ничего не проверив).
Тот же принцип, что и LOCMEM_CACHES ниже (core/tests.py) — тест не должен
зависеть от того, что случайно прописано/не прописано в окружении, где
запускается `manage.py test`.
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
    notify_followers_match_activity,
    notify_prediction_results,
    notify_voting_closing_soon,
    send_notification_digest,
)

User = get_user_model()

# Общий LocMemCache для всех тестов, которые трогают cache.add()-локи —
# прод использует Redis (dopx/settings.py::CACHES), но сама блокировка
# работает через одинаковый Django cache API, а тест не должен требовать
# поднятого Redis (тот же паттерн, что LOCMEM_CACHES в core/tests.py).
LOCMEM_CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "test-notifications-lock",
    }
}

# EMAIL_HOST_USER — см. докстринг модуля: без непустого значения
# _send_email_to_user считает это "консольным dev-режимом" и не пишет в
# mail.outbox вовсе.
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
    """
    notify_followers_match_activity — адресная рассылка ТОЛЬКО подписчикам
    (в отличие от широковещательной send_voting_open_notification), три
    канала разом: in-app Notification, best-effort push, email. Именно
    здесь чинили баг "нет email при открытии голосования на команду,
    на которую подписан пользователь" — эти тесты закрывают его напрямую.
    """

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
        """
        ГЛАВНЫЙ regression-тест: подписчик команды, сыгравшей матч, должен
        получить email именно в момент ОТКРЫТИЯ голосования — до фикса этот
        email никогда не отправлялся (были только in-app+push), хотя про
        ЗАКРЫТИЕ голосования того же матча письмо всегда уходило (см.
        NotifyVotingBothDirectionsRegressionTests ниже — сравнение обеих
        точек жизни голосования).
        """
        result = notify_followers_match_activity(str(self.match.id))

        self.assertEqual(result["notified"], 1)
        self.assertEqual(result["emailed"], 1)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(self.follower.email, mail.outbox[0].to)

        notif = Notification.objects.get(user=self.follower, related_match=self.match)
        self.assertEqual(notif.notification_type, "voting_open")

    def test_non_follower_receives_nothing(self):
        """Пользователь, не подписанный ни на одну из играющих команд, не должен
        получить ни in-app уведомление, ни email — рассылка адресная, не broadcast."""
        notify_followers_match_activity(str(self.match.id))

        self.assertFalse(Notification.objects.filter(user=self.non_follower).exists())
        self.assertEqual(len(mail.outbox), 1)  # только письмо подписчику
        self.assertNotIn(self.non_follower.email, mail.outbox[0].to)

    def test_follower_of_away_team_is_also_notified(self):
        """Follow.team может указывать на ЛЮБУЮ из двух играющих команд, не только home."""
        away_follower = User.objects.create_user(
            username="away_fan", email="away_fan@example.com", password="pass12345", is_verified=True,
        )
        Follow.objects.create(user=away_follower, team=self.away)

        result = notify_followers_match_activity(str(self.match.id))

        self.assertEqual(result["notified"], 2)  # home follower + away follower
        self.assertTrue(Notification.objects.filter(user=away_follower).exists())

    def test_follower_of_player_in_lineup_is_notified(self):
        """Follow может быть на игрока (не команду) — follow-граф проверяет
        состав матча (MatchLineupPlayer), не только home_team/away_team."""
        player = Player.objects.create(first_name="Test", last_name="Player", team=self.home)
        lineup = MatchLineup.objects.create(match=self.match, team=self.home, side="home")
        MatchLineupPlayer.objects.create(lineup=lineup, player=player, is_starting=True)

        player_follower = User.objects.create_user(
            username="player_fan", email="player_fan@example.com", password="pass12345", is_verified=True,
        )
        Follow.objects.create(user=player_follower, player=player)

        result = notify_followers_match_activity(str(self.match.id))

        self.assertTrue(Notification.objects.filter(user=player_follower).exists())
        # follower команды (self.follower) + follower игрока — оба уникальные, без задвоения
        self.assertEqual(result["notified"], 2)

    def test_unverified_follower_gets_inapp_but_no_email(self):
        """
        is_verified=False пропускается ТОЛЬКО почтовым каналом
        (_send_match_email_chunk/`_UserModel.objects.filter(..., is_verified=True,
        email__isnull=False)` в notify_followers_match_activity) — in-app
        Notification создаётся для ВСЕХ подписчиков без разбора, независимо
        от верификации. Проверяем это по факту кода, а не предположению.
        """
        unverified = User.objects.create_user(
            username="unverified", email="unverified@example.com", password="pass12345",
            is_verified=False,
        )
        Follow.objects.create(user=unverified, team=self.home)

        notify_followers_match_activity(str(self.match.id))

        self.assertTrue(Notification.objects.filter(user=unverified).exists())
        all_recipients = [addr for msg in mail.outbox for addr in msg.to]
        self.assertNotIn(unverified.email, all_recipients)

    def test_follower_with_email_channel_disabled_gets_inapp_but_no_email(self):
        """
        Настройка email_match_finished=False (см. NOTIFICATION_TYPE_TO_SETTINGS_KEY
        ['voting_open']) должна отключать именно email-канал, in-app и push
        не завязаны на пользовательские email-настройки вообще.
        """
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
        """
        Push — best-effort канал (см. докстринг notify_followers_match_activity
        и notifications/services.py::send_push_to_user): без VAPID-ключей
        (пусто по умолчанию в dopx/settings.py) send_push_to_user тихо
        возвращает 0 и не должен ронять всю задачу — здесь патчим её, чтобы
        явно убедиться, что вызов происходит для каждого подписчика, а не
        просто "тихо ничего не падает и непонятно, вызывался ли код вообще".
        """
        from unittest.mock import patch

        with patch("notifications.services.send_push_to_user") as mocked_push:
            mocked_push.return_value = 0
            result = notify_followers_match_activity(str(self.match.id))

        self.assertEqual(mocked_push.call_count, 1)  # один подписчик — self.follower
        called_user = mocked_push.call_args.args[0]
        self.assertEqual(called_user.id, self.follower.id)
        self.assertEqual(result["notified"], 1)

    def test_no_match_found_returns_zero_without_error(self):
        """Матч уже удалён/id битый — задача не должна падать, просто no-op."""
        import uuid

        result = notify_followers_match_activity(str(uuid.uuid4()))
        self.assertEqual(result, {"notified": 0})
        self.assertEqual(len(mail.outbox), 0)


@override_settings(**EMAIL_TEST_SETTINGS, CACHES=LOCMEM_CACHES,
                    CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
class NotifyVotingBothDirectionsRegressionTests(TestCase):
    """
    Прямой regression-тест на баг из докстринга notify_followers_match_activity
    (раздел "ИСПРАВЛЕНО", AUDIT_2026-08.md раздел 4): раньше подписчик получал
    email про ЗАКРЫТИЕ голосования (notify_voting_closing_soon — broadcast,
    всем верифицированным), но НИКОГДА про ОТКРЫТИЕ (notify_followers_
    match_activity — раньше только in-app+push). Тест воспроизводит ОБЕ точки
    жизни голосования одного и того же матча и проверяет, что письмо уходит
    в обоих случаях — если кто-то в будущем случайно вернёт email в
    notify_followers_match_activity под force=False с настройкой, которая по
    умолчанию выключена, или уберёт вызов _send_email_to_user, один из этих
    тестов упадёт.
    """

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
        # notify_voting_closing_soon — broadcast всем верифицированным с
        # email, follow здесь не требуется (в отличие от notify_followers_
        # match_activity выше) — именно поэтому раньше их поведение
        # расходилось: закрытие слало письма всем, а открытие — никому.

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
    """
    notify_voting_closing_soon гоняется каждые 30 минут (crontab(minute='*/30')),
    а окно выборки — 1 час: без дедупликации один и тот же закрывающийся
    матч почти всегда попадает в выборку ДВАЖДЫ подряд на соседних тиках и
    письмо уходит всем пользователям дважды (см. "БАГ, КОТОРЫЙ ТУТ БЫЛ" в
    докстринге задачи). Дедуп — по наличию Notification(notification_type=
    'voting_closing', related_match=match), созданного прошлым прогоном.
    """

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
    """
    send_notification_digest — периодическая задача (раз в час), собирающая
    непрочитанные-по-email Notification типов new_badge/level_up/system для
    пользователей с email_digest_mode=True в одно письмо вместо N отдельных.
    Плюс Redis-lock (cache.add()) — БАГ, КОТОРЫЙ ТУТ БЫЛ (см. докстринг
    задачи): без него два параллельных прогона (плановый тик + повторная
    доставка at-least-once) могли прочитать один и тот же набор "ещё не
    отправленных" Notification и разослать дублирующие письма-сводки.
    """

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username="digest_user", email="digest@example.com", password="pass12345",
            is_verified=True,
        )  # email_digest_mode=True по умолчанию (DEFAULT_NOTIFICATION_SETTINGS)

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
        """email_digest_mode=False — пользователь предпочитает мгновенные письма,
        дайджест не должен собирать и отправлять за него сводку задним числом."""
        self.user.notification_settings = {"email_digest_mode": False}
        self.user.save()
        Notification.objects.create(
            user=self.user, notification_type="new_badge", title="Бейдж 1", message="msg1",
        )

        result = send_notification_digest()

        self.assertEqual(result["users_notified"], 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_already_sent_notifications_are_not_included(self):
        """email_sent_at уже проставлен — значит письмо про это уведомление уже
        ушло (мгновенно или прошлым дайджестом), повторно включать в сводку не нужно."""
        Notification.objects.create(
            user=self.user, notification_type="new_badge", title="Старый бейдж", message="msg",
            email_sent_at=timezone.now(),
        )

        result = send_notification_digest()

        self.assertEqual(result, {"users_notified": 0, "notifications_sent": 0})
        self.assertEqual(len(mail.outbox), 0)

    def test_lock_makes_concurrent_run_a_no_op(self):
        """
        Симулируем "другой воркер уже держит лок" явным cache.add() до вызова
        задачи — воспроизводит гонку двух параллельных прогонов детерминированно,
        без реальной многопоточности в тесте.
        """
        Notification.objects.create(
            user=self.user, notification_type="new_badge", title="Бейдж 1", message="msg1",
        )
        cache.add("notifications:lock:send_notification_digest", "1", timeout=600)

        result = send_notification_digest()

        self.assertEqual(result, {"users_notified": 0, "notifications_sent": 0, "skipped_locked": True})
        self.assertEqual(len(mail.outbox), 0)

    def test_lock_is_released_after_run_allowing_next_tick(self):
        """Лок снимается в finally — следующий (не параллельный, а последующий по
        времени) прогон обязан отработать нормально, а не оставаться заблокированным навечно."""
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
    """
    notify_prediction_results — тот же cache.add()-lock-паттерн, что и
    send_notification_digest выше (см. "БАГ, КОТОРЫЙ ТУТ БЫЛ" в докстринге
    задачи): дедуп по Notification(notification_type='prediction_result')
    защищает от задвоения ПОСЛЕ bulk_create, но не от гонки ДО него — два
    параллельных прогона могли одновременно прочитать одну и ту же ещё не
    обработанную пару (match, user) и оба отправить письмо.
    """

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
