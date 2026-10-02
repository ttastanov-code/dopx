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

    def test_free_text_goes_to_claude_with_scoped_tools(self):
        from . import ask

        self.link()
        replies = [
            {"stop_reason": "tool_use", "content": [{"type": "tool_use", "id": "t1", "name": "site_stats", "input": {}}]},
            {"stop_reason": "end_turn", "content": [{"type": "text", "text": "Регистраций 5"}]},
        ]
        with override_settings(ANTHROPIC_API_KEY="k"), mock.patch.object(ask, "_call", side_effect=replies) as call, \
                mock.patch("threading.Thread") as thread:
            handlers.handle(msg("сколько регистраций за неделю?"))
            thread.call_args.kwargs["target"]()
        self.assertIn("Регистраций 5", self.last_text())
        tools = {t["name"] for t in call.call_args_list[0].args[0]["tools"]}
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
        self.assertIn(("🟡 Анонс тура — С одобрением", "d|chan_kind|preview"), [b for row in self.sent[-1][2] for b in row])
        handlers.handle(press("d|chan_kind|preview"))
        labels = [t for row in self.sent[-1][2] for t, _ in row]
        self.assertIn("✓ 🟡 С одобрением", labels)
        cache.set(f"adminbot:elev:dev:{self.staff.pk}", 1, 600)
        with mock.patch.object(handlers, "has_2fa", return_value=True):
            handlers.handle(press("d|chan_mode|preview:auto"))
        self.assertEqual(ChannelConfig.get().mode("preview"), "auto")
        self.assertIn("Анонс тура: 🟢 Сразу", self.last_text())
        self.assertIn(("🟢 Анонс тура — Сразу", "d|chan_kind|preview"), [b for row in self.sent[-1][2] for b in row])


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
