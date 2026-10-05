from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from dashboard.models import StaffAccessGrant
from users.models import User

from . import channel, handlers, incidents
from .models import BotEvent, BotLink, ChannelConfig, ChannelPost, Incident
from .tests import TID, BotTestCase, msg, press


def make_match(**kw):
    from leagues.models import League
    from matches.models import Match
    from seasons.models import Season
    from teams.models import Team

    league = League.objects.create(name=f"КПЛ {Match.objects.count()}", country="KZ")
    season = Season.objects.create(league=league, year="2026", is_active=True)
    start = kw.pop("start_time", timezone.now() - timedelta(hours=3))
    data = dict(league=league, season=season, home_team=Team.objects.create(name="Кайрат"), away_team=Team.objects.create(name="Астана"),
                status="finished", home_score=2, away_score=1, start_time=start, voting_open_until=start + timedelta(days=2))
    data.update(kw)
    return Match.objects.create(**data)


class BotEventTests(TestCase):
    def test_once(self):
        self.assertTrue(BotEvent.once("x"))
        self.assertFalse(BotEvent.once("x"))


class IncidentTests(BotTestCase):
    def setUp(self):
        super().setUp()
        self.me = self.link()
        self.other = User.objects.create_user(username="duty2", email="d2@t.local", password="x", is_staff=True)
        StaffAccessGrant.objects.create(user=self.other, allowed_sections=["admin_bot", "system_status"])
        self.other_link = BotLink.objects.create(user=self.other, telegram_id=TID + 1)

    def test_goes_to_duty_then_escalates_and_ack_stops_it(self):
        BotLink.objects.filter(pk=self.me.pk).update(duty_order=1)
        inc = incidents.open_incident("alert:celery", "Celery", "🔴 Celery лежит")
        self.assertEqual([c for c, *_ in self.sent], [TID])
        self.assertIsNone(incidents.open_incident("alert:celery", "Celery", "снова"))   # без дублей
        Incident.objects.filter(pk=inc.pk).update(next_escalation_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(incidents.escalate_due(), 1)
        self.assertEqual(self.sent[-1][0], TID + 1)
        self.assertIn("не ответил", self.sent[-1][1])
        self.assertIn("Беру", incidents.ack(inc.pk, self.other) + "Беру")
        self.assertIn("Уже в работе", incidents.ack(inc.pk, self.staff))
        self.assertEqual(incidents.escalate_due(), 0)

    def test_without_duty_everyone_gets_it_and_ack_button_works(self):
        inc = incidents.open_incident("surge:1", "Всплеск", "🚨 Всплеск")
        self.assertEqual(sorted(c for c, *_ in self.sent), [TID, TID + 1])
        handlers.handle(press(f"d|inc_ack|{inc.pk}"))
        self.assertTrue(Incident.objects.get(pk=inc.pk).acked_by_id)

    def test_alerts_open_incident_and_resolve(self):
        from . import alerts

        alerts.STATE.clear()
        alerts.MUTED.clear()
        problem = alerts.Problem("celery", "Celery не работает", "нет пульса", True)
        with mock.patch.object(alerts, "current_problems", return_value=[problem]), mock.patch.object(alerts, "enabled", return_value=True):
            alerts.run_checks()
            alerts.run_checks()
        self.assertTrue(Incident.objects.filter(key="alert:celery", resolved_at__isnull=True).exists())
        with mock.patch.object(alerts, "current_problems", return_value=[]):
            alerts.run_checks()
        self.assertTrue(Incident.objects.get(key="alert:celery").resolved_at)
        self.assertIn("Восстановлено", self.sent[-1][1])


@override_settings(ADMIN_BOT_CHANNEL_ID="@dopx_test")
class ChannelTests(BotTestCase):
    def test_modes_dedupe_and_quiet_hours(self):
        cfg = ChannelConfig.get()
        cfg.modes = {"result": "off"}
        cfg.save()
        self.assertIsNone(channel.prepare("result", "k1", "текст"))
        with self.captureOnCommitCallbacks(execute=True):
            post = channel.prepare("preview", "k2", "анонс")
        self.assertEqual(post.status, "draft")
        self.assertIsNone(channel.prepare("preview", "k2", "анонс"))
        cfg.modes = {"preview": "auto"}
        cfg.save()
        night = timezone.make_aware(timezone.datetime(2026, 10, 2, 23, 30))
        with mock.patch("django.utils.timezone.now", return_value=night):
            auto = channel.prepare("preview", "k3", "ночью")
        self.assertEqual(auto.status, "scheduled")
        self.assertEqual(timezone.localtime(auto.scheduled_at).hour, 9)

    def test_publish_once_and_failure(self):
        post = ChannelPost.objects.create(text="<b>Пост</b>", buttons=[["Открыть", "https://dopx.kz/"]])
        with mock.patch("adminbot.telegram.post", return_value={"message_id": 77}) as send:
            ok, _ = channel.publish(post, self.staff)
            again, _ = channel.publish(post, self.staff)
        self.assertTrue(ok)
        self.assertFalse(again)
        self.assertEqual(send.call_count, 1)
        post.refresh_from_db()
        self.assertEqual((post.status, post.message_id), ("published", 77))
        bad = ChannelPost.objects.create(text="x")
        from .telegram import TelegramError
        with mock.patch("adminbot.telegram.post", side_effect=TelegramError(400, "chat not found")):
            ok, message = channel.publish(bad)
        bad.refresh_from_db()
        self.assertFalse(ok)
        self.assertEqual(bad.status, "failed")
        self.assertIn("chat not found", bad.error)

    def test_publish_due(self):
        ChannelPost.objects.create(text="позже", status="scheduled", scheduled_at=timezone.now() + timedelta(hours=1))
        ChannelPost.objects.create(text="сейчас", status="scheduled", scheduled_at=timezone.now() - timedelta(minutes=1))
        with mock.patch("adminbot.telegram.post", return_value={"message_id": 1}):
            self.assertEqual(channel.publish_due(), 1)

    def test_clean_html(self):
        self.assertEqual(channel.clean_html('<b>Жирно</b> <script>x</script> <a href="javascript:1">a</a> <i>не закрыт'),
                         '<b>Жирно</b> &lt;script&gt;x&lt;/script&gt; &lt;a href=&quot;javascript:1&quot;&gt;a&lt;/a&gt; <i>не закрыт</i>')
        self.assertEqual(channel.parse_buttons("Матч | https://dopx.kz/m/\nплохо | ftp://x"), [["Матч", "https://dopx.kz/m/"]])

    def test_bot_new_post_flow_with_2fa_and_photo(self):
        StaffAccessGrant.objects.filter(user=self.staff).update(allowed_sections=["admin_bot", "channel"])
        self.link()
        handlers.handle(press("d|post_new|"))
        self.assertIn("Нет прав", self.last_text())
        self.staff.user_permissions.add(*Permission.objects.filter(codename__in=["add_channelpost", "change_channelpost"]))
        with mock.patch.object(handlers, "has_2fa", return_value=True), mock.patch.object(handlers, "verify_2fa", return_value=True):
            handlers.handle(press("d|post_new|"))
            self.assertIn("код", self.last_text())
            handlers.handle(msg("123456"))
            self.assertIn("Пришлите текст", self.last_text())
            update = msg("")
            update["message"].update(caption="Матч <тура>", photo=[{"file_id": "small"}, {"file_id": "big"}])
            handlers.handle(update)
        post = ChannelPost.objects.get()
        self.assertEqual((post.status, post.tg_file_id, post.text), ("draft", "big", "Матч &lt;тура&gt;"))
        with mock.patch.object(handlers, "has_2fa", return_value=True), \
                mock.patch("adminbot.telegram.send_photo", return_value={"message_id": 5}) as photo:
            handlers.handle(press(f"d|post_pub|{post.pk}"))
        photo.assert_called_once()
        self.assertEqual(ChannelPost.objects.get().status, "published")

    def test_result_post_from_match_finish(self):
        m = make_match(status="live")
        with mock.patch("adminbot.tasks.send_task.delay"), self.captureOnCommitCallbacks(execute=True):
            m.status = "finished"
            m.save()
        self.assertTrue(ChannelPost.objects.filter(key=f"result:{m.pk}", kind="result").exists())


class MatchdayTests(BotTestCase):
    def setUp(self):
        super().setUp()
        StaffAccessGrant.objects.filter(user=self.staff).update(allowed_sections=["admin_bot", "matches", "overview", "antifraud"])
        self.link()
        self.match = make_match()

    def test_match_events_sent_once(self):
        with mock.patch("adminbot.matchday.channel.result_post"), mock.patch("adminbot.tasks.send_task.delay") as delay, \
                self.captureOnCommitCallbacks(execute=True):
            m = make_match(status="scheduled", start_time=timezone.now())
            m.status, m.home_score, m.away_score = "live", 0, 0
            m.save()
            m.status = "finished"
            m.save()
            m.save()
        texts = [c.args[1] for c in delay.call_args_list]
        self.assertEqual(sum("Финал" in t for t in texts), 1)
        self.assertFalse(any("Начался" in t for t in texts))   # тема live выключена по умолчанию

    def test_vote_actions_need_permission_and_change_voting(self):
        cache.set(f"adminbot:elev:dev:{self.staff.pk}", 1, 600)
        with mock.patch.object(handlers, "has_2fa", return_value=True):
            handlers.handle(press(f"d|vote_freeze|{self.match.pk}"))
            self.assertIn("Нет прав", self.last_text())
            self.staff.user_permissions.add(Permission.objects.get(codename="change_match"))
            handlers.handle(press(f"d|vote_freeze|{self.match.pk}"))
            self.match.refresh_from_db()
            self.assertLessEqual(self.match.voting_open_until, timezone.now())
            handlers.handle(press(f"d|vote_more|{self.match.pk}"))
        self.match.refresh_from_db()
        self.assertGreater(self.match.voting_open_until, timezone.now() + timedelta(minutes=55))

    def test_report_and_surge(self):
        from evaluations.models import EvaluationSession

        from .matchday import run_reports, run_surges

        users = [User.objects.create_user(username=f"fan{i}", email=f"f{i}@t.local", password="x") for i in range(45)]
        now = timezone.now()
        for i, u in enumerate(users):
            EvaluationSession.objects.create(user=u, match=self.match, status="completed", completed_at=now - timedelta(minutes=1),
                                             ip_address=f"10.0.0.{i % 200}")
        self.match.start_time = now - timedelta(hours=5)
        self.match.save()
        with mock.patch("adminbot.tasks.send_task.delay") as delay, self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(run_reports(now), 1)
            self.assertEqual(run_reports(now), 0)
        self.assertIn("Оценок: <b>45</b>", delay.call_args.args[1])
        self.assertEqual(run_surges(now), 1)
        self.assertIn("одной подсети", self.sent[-1][1])
        self.assertIn(f"d|vote_freeze|{self.match.pk}", self.buttons())


class ReviewTimingTests(BotTestCase):
    def setUp(self):
        super().setUp()
        self.match = make_match()

    def test_review_waits_for_voting_close(self):
        from aggregates.models import PlayerMatchAggregate
        from players.models import Player

        from .matchday import run_reviews

        now = timezone.now()
        player = Player.objects.create(first_name="А", last_name="Б", team=self.match.home_team)
        PlayerMatchAggregate.objects.create(player=player, match=self.match, performance_score=8, total_votes=50)
        self.match.voting_open_until = now + timedelta(hours=10)
        self.match.save()
        with mock.patch("adminbot.channel.review_post") as review:
            self.assertEqual(run_reviews(now), 0)
            self.match.voting_open_until = now - timedelta(minutes=5)
            self.match.save()
            self.assertEqual(run_reviews(now), 1)
            self.assertEqual(run_reviews(now), 0)
        review.assert_called_once()


class ExpertsAndContactsTests(BotTestCase):
    def setUp(self):
        super().setUp()
        StaffAccessGrant.objects.filter(user=self.staff).update(allowed_sections=["admin_bot", "experts", "data_trust", "matches"])
        self.link()
        self.staff.user_permissions.add(*Permission.objects.filter(codename__in=[
            "add_expertinvite", "change_experttake", "change_contactsubmission"]))
        cache.set(f"adminbot:elev:dev:{self.staff.pk}", 1, 600)

    def test_invite_flow_returns_forwardable_link(self):
        from engagement.models import Expert, ExpertInvite

        e = Expert.objects.create(name="Иван Петров")
        m = make_match(status="scheduled", start_time=timezone.now() + timedelta(days=1))
        with mock.patch.object(handlers, "has_2fa", return_value=True):
            handlers.handle(press(f"d|inv_exp|{e.pk}"))
            handlers.handle(press(f"d|inv_go|{m.pk}"))
        inv = ExpertInvite.objects.get()
        self.assertEqual((inv.expert, inv.match), (e, m))
        self.assertIn(inv.token, self.last_text())
        self.assertIn("Иван Петров", self.last_text())

    def test_take_return_and_contact_reply(self):
        from engagement.models import Expert, ExpertInvite, ExpertTake
        from notifications.models import ContactSubmission, Notification

        m = make_match()
        inv = ExpertInvite.objects.create(expires_at=timezone.now() + timedelta(hours=1))
        take = ExpertTake.objects.create(match=m, expert=Expert.objects.create(name="Э"), text="текст", invite=inv, is_published=False)
        with mock.patch.object(handlers, "has_2fa", return_value=True):
            handlers.handle(press(f"d|take_return|{take.pk}"))
            handlers.handle(msg("Добавьте про второй тайм"))
        take.refresh_from_db()
        self.assertEqual(take.review_note, "Добавьте про второй тайм")
        self.assertIn(inv.token, self.last_text())
        fan = User.objects.create_user(username="fan", email="fan@t.local", password="x")
        c = ContactSubmission.objects.create(user=fan, subject="Счёт", message="Неверный счёт", category="data_error", related_match=m)
        with mock.patch.object(handlers, "has_2fa", return_value=True), mock.patch("notifications.tasks.send_push_task.delay"):
            handlers.handle(press(f"d|contact_reply|{c.pk}"))
            handlers.handle(msg("Исправили, спасибо!"))
        c.refresh_from_db()
        self.assertEqual((c.status, c.admin_response), ("resolved", "Исправили, спасибо!"))
        self.assertTrue(Notification.objects.filter(user=fan, notification_type="contact_reply", message="Исправили, спасибо!").exists())

    def test_cancel_clears_waiting_text(self):
        with mock.patch.object(handlers, "has_2fa", return_value=True):
            handlers.handle(press("d|contact_reply|00000000-0000-0000-0000-000000000000"))
        handlers.handle(msg("/cancel"))
        self.assertIsNone(cache.get(f"adminbot:await:dev:{TID}"))


class SettingsScreensTests(BotTestCase):
    def test_topics_and_duty(self):
        link = self.link()
        handlers.handle(press("d|topic|live"))
        link.refresh_from_db()
        self.assertIn("live", link.topics)
        handlers.handle(press("d|topic|digest"))
        link.refresh_from_db()
        self.assertNotIn("digest", link.topics)
        self.assertFalse(link.wants("digest"))
        handlers.handle(press("d|duty_me|"))
        link.refresh_from_db()
        self.assertEqual(link.duty_order, 1)
        handlers.handle(press("d|duty_clear|"))
        self.assertIn("Нет доступа", self.last_text())

    def test_free_text_goes_to_claude_only_when_chat_enabled(self):
        from types import SimpleNamespace as NS

        from . import ask

        self.link()
        replies = [
            NS(stop_reason="tool_use", content=[NS(type="tool_use", id="t1", name="site_stats", input={})]),
            NS(stop_reason="end_turn", content=[NS(type="text", text="Регистраций 5")]),
        ]
        with override_settings(ANTHROPIC_API_KEY="k", ADMIN_BOT_AI_CHAT=False):
            handlers.handle(msg("сколько регистраций за неделю?"))
        self.assertIn("Привет", self.last_text())                 # чат выключен — просто меню
        with override_settings(ANTHROPIC_API_KEY="k", ADMIN_BOT_AI_CHAT=True), \
                mock.patch("anthropic.Anthropic") as client, mock.patch("threading.Thread") as thread:
            client.return_value.beta.messages.create.side_effect = replies
            handlers.handle(msg("сколько регистраций за неделю?"))
            thread.call_args.kwargs["target"]()
        self.assertIn("Регистраций 5", self.last_text())
        tools = {t["name"] for t in client.return_value.beta.messages.create.call_args_list[0].kwargs["tools"]}
        self.assertIn("site_stats", tools)          # у сотрудника есть «Обзор»
        StaffAccessGrant.objects.filter(user=self.staff).update(allowed_sections=["admin_bot"])
        self.assertNotIn("site_stats", ask._tools_for(User.objects.get(pk=self.staff.pk)))


class ReportsTests(TestCase):
    def test_digest_weekly_and_pdf(self):
        from partners.models import Banner, BannerDailyStat, Partner

        from .reports import digest_text, partner_report_pdf, weekly_staff_text

        User.objects.create_user(username="sleepy", email="s@t.local", password="x", is_staff=True)
        with override_settings(ADMIN_BOT_TOKEN="t"):
            self.assertIn("Сводка", digest_text())
            text, rows = weekly_staff_text()
        self.assertIn("sleepy", text)
        self.assertTrue(rows)
        p = Partner.objects.create(name="Партнёр", slug="partner")
        b = Banner.objects.create(partner=p, zone="home_top", title="Баннер", target_url="https://x.kz")
        BannerDailyStat.objects.create(banner=b, date=timezone.localdate(), impressions=100, clicks=3)
        self.assertTrue(partner_report_pdf(p, 7).startswith(b"%PDF"))

    def test_latency_p95(self):
        from .latency import p95, record

        now = 1_000_000.0
        for _ in range(90):
            record(0.05, now)
        for _ in range(10):
            record(4.0, now)
        self.assertEqual(p95(5, now), (5.0, 100))


@override_settings(STAFF_2FA_ENFORCED=False, ADMIN_BOT_CHANNEL_ID="")
class ChannelDashboardTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="ed", email="ed@t.local", password="x", is_staff=True)
        StaffAccessGrant.objects.create(user=self.user, allowed_sections=["channel"])
        self.client.force_login(self.user)

    def test_view_create_and_modes_permissions(self):
        self.assertEqual(self.client.get(reverse("dashboard:channel")).status_code, 200)
        self.assertEqual(self.client.post(reverse("dashboard:channel"), {"text": "x"}).status_code, 403)
        self.user.user_permissions.add(Permission.objects.get(codename="add_channelpost"))
        self.client.force_login(User.objects.get(pk=self.user.pk))
        self.client.post(reverse("dashboard:channel"), {"text": "<b>Привет</b><script>", "buttons": "DOPX | https://dopx.kz", "action": "draft"})
        post = ChannelPost.objects.get()
        self.assertEqual(post.text, "<b>Привет</b>&lt;script&gt;")
        self.assertEqual(post.buttons, [["DOPX", "https://dopx.kz"]])
        self.assertEqual(self.client.post(reverse("dashboard:channel_modes"), {"mode_result": "auto"}).status_code, 403)
        page = self.client.get(reverse("dashboard:channel_post", args=[post.pk]))
        self.assertContains(page, "Привет")

    def test_calendar(self):
        when = timezone.make_aware(timezone.datetime(2026, 11, 14, 18, 30))
        ChannelPost.objects.create(kind="preview", text="Анонс", status="scheduled", scheduled_at=when)
        ChannelPost.objects.create(kind="manual", text="Черновик", status="draft", scheduled_at=when)
        page = self.client.get(reverse("dashboard:channel") + "?month=2026-11")
        self.assertContains(page, "Ноябрь 2026")
        self.assertContains(page, "18:30 · Превью тура")
        self.assertNotContains(page, "Свой пост · ")
        # Мусор в ?month= — текущий месяц, без ошибки.
        self.assertEqual(self.client.get(reverse("dashboard:channel") + "?month=zzz").status_code, 200)


@override_settings(ADMIN_BOT_CHANNEL_ID="@dopx_kz", ADMIN_BOT_ALLOWED_CHATS="-1009")
class GuardTests(BotTestCase):
    def _added(self, chat):
        return {"update_id": 9, "my_chat_member": {"chat": chat, "from": {"id": 1, "first_name": "Чужой"},
                                                  "new_chat_member": {"status": "administrator"}}}

    def test_leaves_foreign_chats_and_stays_in_ours(self):
        from . import router

        boss = User.objects.create_superuser(username="root", email="r@t.local", password="x")
        BotLink.objects.create(user=boss, telegram_id=TID + 5)
        with mock.patch("adminbot.telegram.call") as call:
            router.route(self._added({"id": -1001, "type": "channel", "username": "DOPX_KZ", "title": "DOPX"}))
            router.route(self._added({"id": -1009, "type": "channel", "title": "Тест"}))
            call.assert_not_called()
            router.route(self._added({"id": -1002, "type": "channel", "username": "spam", "title": "Спам"}))
            router.route(self._added({"id": -1003, "type": "supergroup", "title": "Группа"}))
        self.assertEqual([c.kwargs["chat_id"] for c in call.call_args_list], [-1002, -1003])
        self.assertTrue(all(c.args[0] == "leaveChat" for c in call.call_args_list))
        self.assertEqual(self.sent[-1][0], TID + 5)
        self.assertIn("чужой", self.sent[-1][1])

    @override_settings(ADMIN_BOT_CHANNEL_ID="", ADMIN_BOT_ALLOWED_CHATS="")
    def test_without_configured_channel_does_not_leave_channels(self):
        from . import router

        boss = User.objects.create_superuser(username="root", email="r@t.local", password="x")
        BotLink.objects.create(user=boss, telegram_id=TID + 5)
        with mock.patch("adminbot.telegram.call") as call:
            router.route(self._added({"id": -1001, "type": "channel", "username": "dopx_kz", "title": "DOPX"}))
            call.assert_not_called()
            router.route(self._added({"id": -1003, "type": "group", "title": "Группа"}))
            call.assert_called_once()
        self.assertIn("ADMIN_BOT_CHANNEL_ID", self.sent[0][1])


class ChannelModesScreenTests(BotTestCase):
    def test_list_shows_current_mode_and_kind_screen_switches_it(self):
        StaffAccessGrant.objects.filter(user=self.staff).update(allowed_sections=["admin_bot", "channel"])
        self.link()
        self.staff.user_permissions.add(Permission.objects.get(codename="delete_channelpost"))
        handlers.handle(press("d|chan_modes|"))
        self.assertIn(("🟡 Превью тура — С одобрением", "d|chan_kind|preview"), [b for row in self.sent[-1][2] for b in row])
        handlers.handle(press("d|chan_kind|preview"))
        labels = [t for row in self.sent[-1][2] for t, _ in row]
        self.assertIn("✓ 🟡 С одобрением", labels)
        cache.set(f"adminbot:elev:dev:{self.staff.pk}", 1, 600)
        with mock.patch.object(handlers, "has_2fa", return_value=True):
            handlers.handle(press("d|chan_mode|preview:auto"))
        self.assertEqual(ChannelConfig.get().mode("preview"), "auto")
        self.assertIn("Превью тура: 🟢 Сразу", self.last_text())
        self.assertIn(("🟢 Превью тура — Сразу", "d|chan_kind|preview"), [b for row in self.sent[-1][2] for b in row])


class GitHubDispatchTests(TestCase):
    def _resp(self, code, text=""):
        r = mock.Mock(status_code=code, text=text, content=text.encode())
        r.json.return_value = {}
        return r

    @override_settings(ADMIN_BOT_GITHUB_TOKEN="t", ADMIN_BOT_GITHUB_REPO="o/r")
    def test_old_workflow_without_inputs(self):
        from . import github

        old = self._resp(422, '{"message":"Unexpected inputs provided: [\\"ref\\"]"}')
        with mock.patch("requests.request", side_effect=[old, self._resp(204)]) as req:
            github.dispatch("")
        self.assertNotIn("inputs", req.call_args.kwargs["json"])
        with mock.patch("requests.request", return_value=old), self.assertRaisesMessage(github.GitHubError, "Слейте dev в main"):
            github.dispatch("v1.2.3")
        with mock.patch("requests.request", return_value=self._resp(403)), self.assertRaisesMessage(github.GitHubError, "нет прав"):
            github.dispatch("")


@override_settings(ADMIN_BOT_GITHUB_TOKEN="t", ADMIN_BOT_GITHUB_REPO="o/r")
class DeployWatchTests(BotTestCase):
    def test_bot_reports_run_result_with_skipped_deploy(self):
        self.staff.is_superuser = True
        self.staff.save()
        self.link()
        cache.set(f"adminbot:elev:dev:{self.staff.pk}", 1, 600)
        with mock.patch.object(handlers, "has_2fa", return_value=True), mock.patch("adminbot.github.dispatch"):
            handlers.handle(press("d|deploy_go|main"))
        self.assertIn("Слежу за запуском", self.last_text())
        running = {"status": "in_progress", "conclusion": None, "url": "https://github.com/o/r/actions/runs/1", "jobs": [], "deploy_skipped": False}
        done = {"status": "completed", "conclusion": "success", "url": "https://github.com/o/r/actions/runs/1",
                "jobs": [("Тесты", "success"), ("Деплой на прод", "success")], "deploy_skipped": True}
        with mock.patch("adminbot.github.find_dispatched", return_value={"id": 1}) as find, \
                mock.patch("adminbot.github.run_report", side_effect=[running, done]):
            self.assertEqual(handlers.check_deploys(), 0)
            self.assertEqual(handlers.check_deploys(), 1)
        self.assertEqual(find.call_count, 1)               # id запуска запомнили
        self.assertIn("сервер ещё не подключён", self.last_text())
        self.assertEqual(handlers.check_deploys(), 0)


@override_settings(STAFF_2FA_ENFORCED=False, ADMIN_BOT_TOKEN="t", ADMIN_BOT_ENABLED=True)
class BotDebugPageTests(TestCase):
    def test_staff_sees_plain_page_superuser_sees_debug(self):
        import logging

        from . import debug

        staff = User.objects.create_user(username="mod", email="m@t.local", password="x", is_staff=True)
        StaffAccessGrant.objects.create(user=staff, allowed_sections=["admin_bot"])
        info = {"ok": True, "username": "dopx_bot", "webhook": "", "pending": 0, "last_error": ""}
        with mock.patch("adminbot.debug.telegram_info", return_value=info):
            self.client.force_login(staff)
            page = self.client.get(reverse("dashboard:admin_bot")).content.decode()
            for word in ("ADMIN_BOT", ".env", "runbot", "Журнал", "прод"):
                self.assertNotIn(word, page)
            self.assertIn("Привязать Telegram", page)
            logging.getLogger("adminbot.handlers").error("Тестовая ошибка бота")
            boss = User.objects.create_superuser(username="root", email="r@t.local", password="x")
            self.client.force_login(boss)
            page = self.client.get(reverse("dashboard:admin_bot"))
        self.assertContains(page, "Журнал ошибок бота")
        self.assertContains(page, "Тестовая ошибка бота")
        debug.beat("listener")
        self.assertTrue(debug.heartbeat()["alive"])
        with mock.patch("adminbot.telegram.send", return_value={"message_id": 1}) as send:
            BotLink.objects.create(user=boss, telegram_id=1)
            self.client.post(reverse("dashboard:admin_bot"), {"action": "dbg_test_message"})
        send.assert_called_once()


class SamplePostsTests(TestCase):
    def test_samples_are_drafts_from_real_data(self):
        m = make_match()
        make_match(status="scheduled", start_time=timezone.now() + timedelta(days=1), tour=5)
        posts = channel.sample_posts()
        kinds = {p.kind for p in posts}
        self.assertTrue({"preview", "result"} <= kinds)
        self.assertTrue(all(p.status == "draft" for p in posts))
        self.assertTrue(any("Кайрат 2:1 Астана" in p.text for p in posts if p.kind == "result"))
        self.assertEqual(len(channel.sample_posts()), len(posts))   # повторно — новые черновики, без конфликта ключей


@override_settings(ADMIN_BOT_CHANNEL_ID="@dopx_test", ADMIN_BOT_TOKEN="t", ADMIN_BOT_ENABLED=True)
class KickoffAndUnpublishTests(TestCase):
    def test_placeholder_time_and_unpublish(self):
        from datetime import timezone as dt_tz

        m = make_match(status="scheduled", start_time=timezone.datetime(2026, 10, 10, tzinfo=dt_tz.utc))
        self.assertEqual(channel.kickoff(m), "10.10 (время уточняется)")
        m.start_time = timezone.datetime(2026, 10, 10, 13, 0, tzinfo=dt_tz.utc)
        self.assertEqual(channel.kickoff(m), "10.10 18:00")
        self.assertFalse(any(p.kind == "changes" for p in channel.sample_posts()))   # переносов нет — пробного не будет
        post = ChannelPost.objects.create(text="x", status="published", message_id=42)
        with mock.patch("adminbot.telegram.call") as call:
            ok, _ = channel.unpublish(post)
        call.assert_called_once_with("deleteMessage", chat_id="@dopx_test", message_id=42)
        post.refresh_from_db()
        self.assertTrue(ok)
        self.assertEqual(post.status, "cancelled")


class ChannelLinkTests(TestCase):
    @override_settings(ADMIN_BOT_PUBLIC_URL="https://dopx.kz")
    def test_links_point_to_public_site_and_appear_in_text(self):
        m = make_match()
        post = channel.result_post(m, sample=True)
        url = f"https://dopx.kz/matches/{m.pk}/"
        self.assertEqual(post.buttons, [["⭐ Оценить матч", url]])
        self.assertIn(f'👉 <a href="{url}">⭐ Оценить матч</a>\n\n#DOPX', post.text)   # ссылка перед хештегами


class ReadableButtonsTests(BotTestCase):
    def test_fit_rows(self):
        from .telegram import fit_rows

        rows = fit_rows([[("Иртыш – Кызыл-Жар", "a"), ("Женис – Атырау", "b")], [("Да", "c"), ("Нет", "d")],
                         [("Очень длинная надпись на кнопке, которая не влезет никуда вообще", "e")]])
        self.assertEqual(rows[0], [("Иртыш – Кызыл-Жар", "a")])     # не влезло вдвоём — по одной
        self.assertEqual(rows[2], [("Да", "c"), ("Нет", "d")])      # короткие остаются рядом
        self.assertTrue(rows[3][0][0].endswith("…"))

    def test_no_screen_has_cut_labels(self):
        from .telegram import ROW_FIT, fit_rows, text_width

        self.staff.is_superuser = True
        self.staff.save()
        self.link()
        make_match(status="scheduled", start_time=timezone.now() + timedelta(hours=5))
        screens = ["menu", "md", "sum", "status", "chan", "chan_modes", "chan_kind|preview", "exp", "inv_new", "mourn",
                   "partners", "duty", "topics", "scripts", "takes", "flags", "names", "dups", "contacts"]
        for screen in screens:
            action, _, arg = screen.partition("|")
            handlers.handle(press(f"d|{action}|{arg}"))
            for row in fit_rows(self.sent[-1][2] or []):
                for label, _data in row:
                    self.assertFalse(label.endswith("…"), f"{screen}: обрезано «{label}»")
                    self.assertLessEqual(text_width(label), ROW_FIT[len(row)] + 4, f"{screen}: не влезает «{label}»")


class WriterAndFormatsTests(TestCase):
    def _response(self, text):
        from types import SimpleNamespace as NS

        return NS(stop_reason="end_turn", content=[NS(type="text", text=text)])

    @override_settings(ANTHROPIC_API_KEY="k", ADMIN_BOT_AI_POSTS=True)
    def test_claude_text_used_when_numbers_are_from_facts(self):
        from . import writer

        cache.clear()
        facts = {"счёт": "2:1", "минута": "89"}
        with mock.patch("anthropic.Anthropic") as client:
            client.return_value.beta.messages.create.return_value = self._response("<b>Гол на 89-й</b> — 2:1")
            self.assertEqual(writer.write("review", facts, lambda: "шаблон"), ("<b>Гол на 89-й</b> — 2:1", True))
            client.return_value.beta.messages.create.return_value = self._response("Победа 2:1, 15 ударов")
            self.assertEqual(writer.write("review", facts, lambda: "шаблон"), ("шаблон", False))   # 15 — выдумка

    @override_settings(ANTHROPIC_API_KEY="k", ADMIN_BOT_AI_POSTS=True)
    def test_no_balance_switches_to_templates_for_hours(self):
        import anthropic
        import httpx2

        from . import writer

        cache.clear()
        request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
        error = anthropic.BadRequestError("Your credit balance is too low", response=httpx2.Response(400, request=request), body=None)
        with mock.patch("anthropic.Anthropic") as client:
            client.return_value.beta.messages.create.side_effect = error
            self.assertEqual(writer.write("review", {}, lambda: "шаблон"), ("шаблон", False))
            self.assertFalse(writer.ai_enabled())
            writer.write("review", {}, lambda: "шаблон")
        self.assertEqual(client.return_value.beta.messages.create.call_count, 1)    # второй раз API не дёргали
        self.assertIn("баланс", writer.status()["paused"]["Claude"])

    @override_settings(ADMIN_BOT_CHANNEL_ID="@dopx_test", ADMIN_BOT_TOKEN="t", ADMIN_BOT_ENABLED=True, ANTHROPIC_API_KEY="")
    def test_review_preview_poll_and_album_publish(self):
        from events.models import MatchEvent

        m = make_match()
        MatchEvent.objects.create(match=m, minute=89, event_type="goal", team_side="home", score_after="2-1")
        cfg = ChannelConfig.get()
        cfg.modes = {"ratings": "auto", "preview": "auto", "poll": "auto"}
        cfg.quiet_hours = False
        cfg.save()
        review = channel.review_post(m)
        self.assertIn("2:1", review.text)
        self.assertFalse(review.by_ai)
        nxt = make_match(status="scheduled", start_time=timezone.now() + timedelta(hours=20), tour=7)
        channel.preview_post(7, [nxt])
        poll = ChannelPost.objects.get(kind="poll")
        self.assertEqual(poll.poll["options"][1], "Ничья")
        with mock.patch("adminbot.telegram.call", return_value={"message_id": 9}) as call, \
                mock.patch("adminbot.telegram.post", return_value={"message_id": 8}):
            ok, _ = channel.publish(poll)
        self.assertTrue(ok)
        self.assertEqual(call.call_args.args[0], "sendPoll")
        album = ChannelPost.objects.create(text="Итоги", image="a.png", images=["b.png"])
        with mock.patch("adminbot.channel._read", return_value=b"img"), \
                mock.patch("adminbot.telegram.call_files", return_value=[{"message_id": 5}]) as files:
            ok, _ = channel.publish(album)
        self.assertTrue(ok)
        self.assertEqual(files.call_args.args[0], "sendMediaGroup")
        self.assertEqual(len(files.call_args.kwargs["media"]), 2)


class TokenRedactionTests(TestCase):
    def test_token_never_reaches_log_or_journal(self):
        import logging

        from . import debug

        cache.clear()
        token = "123456789:AAfakeTESTtokenTESTtokenTESTtoken"
        logging.getLogger("adminbot.handlers").error("сеть: Max retries exceeded with url: /bot%s/getUpdates", token)
        text = debug.recent_log()[0]["text"]
        self.assertNotIn("AAfake", text)
        self.assertIn("bot<токен>", text)

    @override_settings(ADMIN_BOT_CHANNEL_ID="@dopx_test", ADMIN_BOT_TOKEN="t", ADMIN_BOT_ENABLED=True)
    def test_unpublish_post_already_deleted_in_channel(self):
        from .telegram import TelegramError

        post = ChannelPost.objects.create(text="x", status="published", message_id=7)
        with mock.patch("adminbot.telegram.call", side_effect=TelegramError(400, "Bad Request: message to delete not found")):
            ok, message = channel.unpublish(post)
        post.refresh_from_db()
        self.assertTrue(ok)
        self.assertIn("уже нет", message)
        self.assertEqual(post.status, "cancelled")
        old = ChannelPost.objects.create(text="y", status="published")
        self.assertTrue(channel.unpublish(old)[0])

    @override_settings(ADMIN_BOT_TOKEN="123456789:AAfakeTESTtokenTESTtokenTESTtoken")
    def test_network_error_has_no_token(self):
        import requests

        from . import telegram as tg

        with mock.patch("requests.post", side_effect=requests.ConnectionError(
                "Max retries exceeded with url: /bot123456789:AAfakeTESTtokenTESTtokenTESTtoken/getUpdates")):
            with self.assertRaises(requests.ConnectionError) as ctx:
                tg.call("getUpdates")
        self.assertNotIn("AAfake", str(ctx.exception))


class BotFlagsTests(BotTestCase):
    def test_flags_switch_without_restart(self):
        from . import alerts, ask, flags, writer

        self.staff.is_superuser = True
        self.staff.save()
        self.link()
        with override_settings(ANTHROPIC_API_KEY="k", ADMIN_BOT_AI_POSTS=True, ADMIN_BOT_AI_CHAT=False, ADMIN_BOT_ALERTS="auto"):
            self.assertTrue(writer.ai_enabled())
            self.assertFalse(ask.enabled())
            cache.set(f"adminbot:elev:dev:{self.staff.pk}", 1, 600)
            with mock.patch.object(handlers, "has_2fa", return_value=True):
                handlers.handle(press("d|flag|bot_ai_posts"))
                handlers.handle(press("d|flag|bot_ai_chat"))
                handlers.handle(press("d|flag|bot_alerts:on"))
            self.assertIn("Сообщения о сбоях сервера: Включены везде", self.last_text())
            self.assertFalse(writer.ai_enabled())          # выключили — сразу шаблоны
            self.assertTrue(ask.enabled())
            self.assertTrue(alerts.enabled())              # dev, но «on»
        changed = {"bot_ai_posts", "bot_ai_chat", "bot_alerts"}
        self.assertFalse(any(f["from_env"] for f in flags.overview() if f["key"] in changed))

    @override_settings(STAFF_2FA_ENFORCED=False)
    def test_dashboard_form_on_platform_settings_needs_permission(self):
        from . import flags

        StaffAccessGrant.objects.filter(user=self.staff).update(allowed_sections=["platform_settings"])
        self.client.force_login(self.staff)
        self.assertEqual(self.client.post(reverse("dashboard:platform_flags_save"), {"bot_ai_chat": "on"}).status_code, 403)
        self.assertFalse(flags.get("bot_ai_chat"))
        self.staff.user_permissions.add(*Permission.objects.filter(codename__in=["view_platformsetting", "change_platformsetting"]))
        self.client.force_login(User.objects.get(pk=self.staff.pk))
        self.client.post(reverse("dashboard:platform_flags_save"), {"bot_ai_chat": "on", "bot_alerts": "off", "names_ai_provider": "claude"})
        page = self.client.get(reverse("dashboard:platform_settings"))
        self.assertContains(page, "ИИ и оповещения")
        self.assertNotContains(page, "bot_ai_chat</")          # не дублируется в общей таблице
        self.assertTrue(flags.get("bot_ai_chat"))
        self.assertFalse(flags.get("bot_ai_posts"))
        self.assertEqual((flags.get("bot_alerts"), flags.get("names_ai_provider")), ("off", "claude"))


class ProviderSwitchTests(BotTestCase):
    @override_settings(ANTHROPIC_API_KEY="a", GEMINI_API_KEY="g", ADMIN_BOT_AI_POSTS=True, POSTS_AI_PROVIDER="claude")
    def test_articles_fall_back_to_second_provider_then_template(self):
        from core import llm

        from . import flags, writer

        cache.clear()
        calls = []

        def fake(provider, system, prompt, **kw):
            calls.append(provider)
            if provider == "claude":
                raise llm.LLMError("billing", "credit balance is too low")
            return "<b>Счёт 2:1</b>"
        with mock.patch("core.llm.generate", side_effect=fake):
            self.assertEqual(writer.write("review", {"счёт": "2:1"}, lambda: "шаблон"), ("<b>Счёт 2:1</b>", True))
            self.assertEqual(calls, ["claude", "gemini"])
            self.assertEqual(writer.providers(), ["gemini"])           # Claude на паузе после баланса
            flags.set_flag("posts_ai_provider", "gemini", None)
            calls.clear()
            writer.write("review", {"счёт": "2:1"}, lambda: "шаблон")
            self.assertEqual(calls, ["gemini"])
        with mock.patch("core.llm.generate", side_effect=llm.LLMError("temporary", "down")):
            self.assertEqual(writer.write("review", {}, lambda: "шаблон"), ("шаблон", False))

    @override_settings(ANTHROPIC_API_KEY="a", GEMINI_API_KEY="g", NAMES_AI_PROVIDER="gemini")
    def test_names_use_selected_provider_with_failover(self):
        from core import llm
        from parsers import name_ai

        from . import flags

        cache.clear()
        answer = '{"first_name": "Темирлан", "last_name": "Ерланов", "confidence": "high", "matches_current": true, "reasoning": "КПЛ"}'
        with mock.patch("core.llm.generate", side_effect=[llm.LLMError("temporary", "503"), "Нашёл: " + answer]) as gen:
            result = name_ai.verify_name("игрока", "Темирлан", "Ерланов")
        self.assertTrue(result.ok)
        self.assertEqual(result.provider, "claude")
        self.assertEqual([c.args[0] for c in gen.call_args_list], ["gemini", "claude"])
        self.assertTrue(gen.call_args.kwargs["web_search"])
        flags.set_flag("names_ai_provider", "claude", None)
        self.assertEqual(name_ai.provider_label(), "Claude")

    def test_bot_settings_screen_switches_provider(self):
        from . import flags

        self.staff.is_superuser = True
        self.staff.save()
        self.link()
        handlers.handle(press("d|bot_settings|"))
        self.assertIn(("🔎 ИИ для ФИО: Gemini", "d|setting|names_ai_provider"), [b for r in self.sent[-1][2] for b in r])
        handlers.handle(press("d|setting|names_ai_provider"))
        self.assertIn("Какой ИИ проверяет ФИО", self.last_text())
        cache.set(f"adminbot:elev:dev:{self.staff.pk}", 1, 600)
        with mock.patch.object(handlers, "has_2fa", return_value=True):
            handlers.handle(press("d|flag|names_ai_provider:claude"))
        self.assertEqual(flags.get("names_ai_provider"), "claude")
        self.assertIn("Какой ИИ проверяет ФИО: Claude", self.last_text())
        self.assertIn(("🔎 ИИ для ФИО: Claude", "d|setting|names_ai_provider"), [b for r in self.sent[-1][2] for b in r])


class SafeModeTests(BotTestCase):
    @override_settings(DEV_SAFE_MODE=True, DEV_SAFE_ALLOW={"channel"}, ANTHROPIC_API_KEY="a", GEMINI_API_KEY="g",
                       ADMIN_BOT_CHANNEL_ID="@dopx_test", ADMIN_BOT_AI_POSTS=True, ADMIN_BOT_AI_CHAT=True)
    def test_everything_external_off_except_allowed(self):
        from core import llm
        from core.safe_mode import overview
        from parsers import name_ai
        from parsers.sportmonks.client import SportmonksAPIError, SportmonksClient
        from parsers.sportmonks.tasks import _sync_enabled
        from notifications.services import _push_ready

        from . import ask, writer

        self.assertFalse(_sync_enabled())
        with mock.patch("requests.Session.get") as get, self.assertRaises(SportmonksAPIError):
            SportmonksClient(api_token="t").get_league(1)
        get.assert_not_called()
        with mock.patch("anthropic.Anthropic") as client:
            self.assertEqual(writer.write("review", {}, lambda: "шаблон"), ("шаблон", False))
            self.assertFalse(name_ai.verify_name("игрока", "А", "Б").ok)
            with self.assertRaises(llm.LLMError):
                llm.generate("claude", "s", "p")
        client.assert_not_called()
        self.assertFalse(ask.enabled())
        self.assertTrue(channel.configured())           # канал разрешён в DEV_SAFE_ALLOW
        self.assertFalse(_push_ready())
        self.assertIn("безопасный режим", handlers.header())
        self.assertEqual({k["key"] for k in overview() if k["allowed"]}, {"channel"})

    @override_settings(DEV_SAFE_MODE=False, ANTHROPIC_API_KEY="a")
    def test_off_by_default_changes_nothing(self):
        from core import llm

        self.assertTrue(llm.configured("claude"))
        self.assertNotIn("безопасный", handlers.header())
