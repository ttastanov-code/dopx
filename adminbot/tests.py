from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from dashboard.models import StaffAccessGrant, StaffActionLog
from users.models import User

from . import handlers, relay, router
from .models import BotLink, BotLinkCode

SECRET = "s" * 32
TID = 555001


def msg(text, tid=TID, chat_type="private"):
    return {"update_id": 1, "message": {"chat": {"id": tid, "type": chat_type}, "from": {"id": tid, "first_name": "Тимур"}, "text": text}}


def press(data, tid=TID):
    return {"update_id": 2, "callback_query": {"id": "cbq", "data": data, "from": {"id": tid},
                                               "message": {"message_id": 10, "chat": {"id": tid, "type": "private"}}}}


@override_settings(ADMIN_BOT_TOKEN="test-token", ADMIN_BOT_ENABLED=True, ADMIN_BOT_RELAY_SECRET=SECRET,
                   STAFF_2FA_ENFORCED=False)
class BotTestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.sent = []
        self.answers = []
        patches = [
            mock.patch("adminbot.telegram.send", side_effect=lambda chat, text, rows=None, silent=False: self.sent.append((chat, text, rows)) or {"ok": 1}),
            mock.patch("adminbot.telegram.edit", side_effect=lambda chat, mid, text, rows=None: self.sent.append((chat, text, rows))),
            mock.patch("adminbot.telegram.answer", side_effect=lambda cid, text="", alert=False: self.answers.append(text)),
            mock.patch.object(handlers, "ENV", "dev"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.staff = User.objects.create_user(username="boss", email="b@t.local", password="x", is_staff=True)
        StaffAccessGrant.objects.create(user=self.staff, allowed_sections=["admin_bot", "overview", "experts", "system_status"])

    def link(self, user=None):
        return BotLink.objects.create(user=user or self.staff, telegram_id=TID)

    def last_text(self):
        return self.sent[-1][1] if self.sent else ""

    def buttons(self):
        rows = self.sent[-1][2] or []
        return [d for row in rows for _, d in row]


class LinkingTests(BotTestCase):
    def test_unlinked_gets_instructions_and_group_chats_ignored(self):
        handlers.handle(msg("/start"))
        self.assertIn("/link", self.last_text())
        n = len(self.sent)
        handlers.handle(msg("/start", chat_type="group"))
        self.assertEqual(len(self.sent), n)

    def test_link_with_code(self):
        code = BotLinkCode.objects.create(user=self.staff)
        handlers.handle(msg(f"/link {code.code}"))
        self.assertTrue(BotLink.objects.filter(user=self.staff, telegram_id=TID).exists())
        texts = " ".join(t for _c, t, _r in self.sent)
        self.assertIn("привязан", texts)
        self.assertIn("2FA", texts)  # предупреждение: 2FA не включена
        self.assertIn("Привет", self.last_text())  # сразу меню
        code.refresh_from_db()
        self.assertIsNotNone(code.used_at)
        # Повторно тот же код не работает.
        BotLink.objects.all().delete()
        handlers.handle(msg(f"/link {code.code}"))
        self.assertFalse(BotLink.objects.exists())

    def test_expired_code_and_non_staff(self):
        code = BotLinkCode.objects.create(user=self.staff)
        BotLinkCode.objects.filter(pk=code.pk).update(created_at=timezone.now() - timedelta(minutes=11))
        handlers.handle(msg(f"/link {code.code}"))
        self.assertFalse(BotLink.objects.exists())
        fan = User.objects.create_user(username="fan", email="f@t.local", password="x")
        code2 = BotLinkCode.objects.create(user=fan)
        handlers.handle(msg(f"/link {code2.code}"))
        self.assertFalse(BotLink.objects.exists())


class MenuAndActionsTests(BotTestCase):
    def setUp(self):
        super().setUp()
        from leagues.models import League
        from matches.models import Match
        from seasons.models import Season
        from teams.models import Team

        from engagement.models import Expert, ExpertInvite, ExpertTake

        lg = League.objects.create(name="КПЛ", country="KZ")
        season = Season.objects.create(league=lg, year="2026", is_active=True)
        start = timezone.now() - timedelta(hours=3)
        match = Match.objects.create(league=lg, season=season, home_team=Team.objects.create(name="А"), away_team=Team.objects.create(name="Б"),
                                     status="finished", start_time=start, voting_open_until=start + timedelta(days=2))
        invite = ExpertInvite.objects.create(match=match, expires_at=timezone.now() + timedelta(days=1))
        with mock.patch("adminbot.notify.push"):
            self.take = ExpertTake.objects.create(match=match, expert=Expert.objects.create(name="Эксперт"), text="Мнение",
                                                  invite=invite, is_published=False)
        self.link()

    def grant(self, *codenames):
        self.staff.user_permissions.add(*Permission.objects.filter(codename__in=codenames))
        self.staff = User.objects.get(pk=self.staff.pk)

    def test_menu_shows_only_allowed_sections(self):
        handlers.handle(msg("/menu"))
        self.assertIn("d|takes|", self.buttons())      # раздел открыт — смотреть можно
        self.assertNotIn("d|flags|", self.buttons())   # нет раздела antifraud
        self.assertIn("env|prod", self.buttons())

    def test_summary_and_status_screens(self):
        handlers.handle(press("d|sum|"))
        self.assertIn("Сводка", self.last_text())
        self.assertIn("Мнения экспертов: 1", self.last_text())
        handlers.handle(press("d|status|"))
        self.assertIn("Сервер", self.last_text())

    def test_write_requires_permission_2fa_and_code(self):
        self.grant("view_experttake")
        handlers.handle(press(f"d|take_pub|{self.take.pk}"))
        self.assertIn("Нет прав", self.last_text())
        self.grant("change_experttake")
        with mock.patch.object(handlers, "has_2fa", return_value=False):
            handlers.handle(press(f"d|take_pub|{self.take.pk}"))
        self.assertIn("2FA", self.last_text())
        with mock.patch.object(handlers, "has_2fa", return_value=True):
            handlers.handle(press(f"d|take_pub|{self.take.pk}"))
            self.assertIn("код", self.last_text())
            self.take.refresh_from_db()
            self.assertFalse(self.take.is_published)
            with mock.patch.object(handlers, "verify_2fa", return_value=False):
                handlers.handle(msg("123456"))
            self.assertIn("неверный", self.last_text())
            with mock.patch.object(handlers, "verify_2fa", return_value=True):
                handlers.handle(msg("654321"))
        self.take.refresh_from_db()
        self.assertTrue(self.take.is_published)
        self.assertTrue(StaffActionLog.objects.filter(action="bot_action", actor=self.staff).exists())

    def test_elevation_skips_code_for_ten_minutes(self):
        self.grant("view_experttake", "change_experttake", "delete_experttake")
        cache.set(f"adminbot:elev:dev:{self.staff.pk}", 1, 600)
        with mock.patch.object(handlers, "has_2fa", return_value=True):
            handlers.handle(press(f"d|take_del|{self.take.pk}"))
        self.assertFalse(type(self.take).objects.filter(pk=self.take.pk).exists())

    def test_callback_for_other_env_is_ignored(self):
        self.grant("view_experttake", "change_experttake")
        n = len(self.sent)
        handlers.handle(press(f"p|take_pub|{self.take.pk}"))
        self.assertEqual(len(self.sent), n)


class RouterTests(BotTestCase):
    def test_prod_listener_routes_by_env(self):
        with mock.patch.object(handlers, "ENV", "prod"), mock.patch("adminbot.handlers.handle") as local:
            # DEV не в сети: переключение не даётся.
            self.assertIsNone(router.route(press("env|dev")))
            self.assertIn("не в сети", self.answers[-1])
            # Агент в сети: переключение и пересылка.
            relay.mark_agent("dev")
            self.assertEqual(router.route(press("env|dev")), "dev")
            self.assertEqual(router.route(msg("/menu")), "dev")
            self.assertEqual(relay.pop_all("dev", wait=0)[-1]["message"]["text"], "/menu")
            # Кнопка из prod-сообщения — локально, даже если выбран DEV.
            self.assertEqual(router.route(press("p|sum|")), "prod")
            self.assertTrue(local.called)
            # Код 2FA уходит туда, где нажали кнопку последней.
            router.route(press("d|take_pub|x"))
            self.assertEqual(router.route(msg("123456")), "dev")

    def test_dev_listener_without_prod(self):
        with mock.patch("adminbot.handlers.handle") as local:
            self.assertEqual(router.route(msg("/start")), "dev")
            self.assertTrue(local.called)
            self.assertIsNone(router.route(press("env|prod")))


class RelayViewTests(BotTestCase):
    def test_secret_ping_and_updates(self):
        url = reverse("adminbot_relay")
        with mock.patch.object(handlers, "ENV", "prod"), override_settings(ADMIN_BOT_ENV="prod"):
            self.assertEqual(self.client.get(url, {"env": "dev", "ping": 1}).status_code, 403)
            self.assertEqual(self.client.get(url, {"env": "dev", "ping": 1}, HTTP_X_RELAY_KEY="wrong").status_code, 403)
            self.assertEqual(self.client.get(url, {"env": "prod"}, HTTP_X_RELAY_KEY=SECRET).status_code, 400)
            relay.mark_listener("prod")
            ping = self.client.get(url, {"env": "dev", "ping": 1}, HTTP_X_RELAY_KEY=SECRET).json()
            self.assertEqual(ping["listener"], "prod")
            relay.push("dev", {"update_id": 7})
            with mock.patch("adminbot.relay.LONG_POLL", 0):
                data = self.client.get(url, {"env": "dev"}, HTTP_X_RELAY_KEY=SECRET).json()
            self.assertEqual(data["updates"], [{"update_id": 7}])
            self.assertTrue(relay.agent_online("dev"))

    def test_short_secret_rejected(self):
        with override_settings(ADMIN_BOT_RELAY_SECRET="short"):
            self.assertFalse(relay.secret_ok("short"))


class NotifyAndDecideTests(BotTestCase):
    def test_contact_submission_pushes_to_permitted_staff(self):
        from notifications.models import ContactSubmission

        self.link()
        StaffAccessGrant.objects.filter(user=self.staff).update(allowed_sections=["admin_bot", "data_trust"])
        self.staff.user_permissions.add(Permission.objects.get(codename="view_contactsubmission"))
        with mock.patch("adminbot.tasks.send_task.delay") as delay, self.captureOnCommitCallbacks(execute=True):
            ContactSubmission.objects.create(guest_email="g@t.local", subject="Ошибка в составе", message="Текст")
        ids, text, rows = delay.call_args.args
        self.assertEqual(ids, [TID])
        self.assertIn("Ошибка в составе", text)

    def test_runbot_role(self):
        from adminbot.management.commands.runbot import Command

        cmd = Command()
        with mock.patch.object(handlers, "ENV", "prod"):
            self.assertEqual(cmd.decide(), "listener")
        with mock.patch("adminbot.relay.hub_status", return_value={"listener": "prod"}):
            self.assertEqual(cmd.decide(), "agent")
        with mock.patch("adminbot.relay.hub_status", return_value=None):
            self.assertEqual(cmd.decide(), "listener")


class DashboardPageTests(BotTestCase):
    def test_code_and_unlink(self):
        StaffAccessGrant.objects.filter(user=self.staff).update(allowed_sections=["admin_bot"])
        self.client.force_login(self.staff)
        page = self.client.post(reverse("dashboard:admin_bot"), {"action": "code"})
        code = BotLinkCode.objects.get(user=self.staff)
        self.assertContains(page, code.code)
        self.link()
        self.assertEqual(self.client.post(reverse("dashboard:admin_bot_unlink"), {"link": "all"}).status_code, 403)
        self.client.post(reverse("dashboard:admin_bot_unlink"), {"link": BotLink.objects.get().pk})
        self.assertFalse(BotLink.objects.exists())


class TelegramClientTests(TestCase):
    @override_settings(ADMIN_BOT_TOKEN="t")
    def test_get_updates_long_poll_params(self):
        from . import telegram as tg

        with mock.patch("adminbot.telegram.requests.post") as post:
            post.return_value.json.return_value = {"ok": True, "result": [{"update_id": 1}]}
            self.assertEqual(tg.get_updates(5, timeout=25), [{"update_id": 1}])
        kwargs = post.call_args.kwargs
        self.assertEqual(kwargs["json"]["timeout"], 25)     # долгий опрос у Telegram
        self.assertEqual(kwargs["json"]["offset"], 5)
        self.assertEqual(kwargs["timeout"], 35)              # HTTP-таймаут с запасом

    @override_settings(ADMIN_BOT_TOKEN="t")
    def test_conflict_raises_409(self):
        from . import telegram as tg

        with mock.patch("adminbot.telegram.requests.post") as post:
            post.return_value.json.return_value = {"ok": False, "error_code": 409, "description": "Conflict"}
            with self.assertRaises(tg.TelegramError) as ctx:
                tg.get_updates(None)
        self.assertEqual(ctx.exception.code, 409)


class KeyboardTests(TestCase):
    def test_local_links_dropped(self):
        from . import telegram as tg

        kb = tg.keyboard([[("Меню", "d|menu|"), ("Дашборд", "http://127.0.0.1:8000/staff/")], [("Сайт", "http://localhost/")],
                          [("Прод", "https://dopx.kz/staff/")]])["inline_keyboard"]
        self.assertEqual(kb, [[{"text": "Меню", "callback_data": "d|menu|"}], [{"text": "Прод", "url": "https://dopx.kz/staff/"}]])


class AccessTests(BotTestCase):
    def test_bot_section_required_on_every_message(self):
        self.link()
        handlers.handle(msg("/menu"))
        self.assertIn("Привет", self.last_text())
        StaffAccessGrant.objects.filter(user=self.staff).update(allowed_sections=["overview"])
        handlers.handle(msg("/menu"))
        self.assertIn("выключен", self.last_text())
        handlers.handle(press("d|sum|"))
        self.assertIn("выключен", self.last_text())

    def test_link_code_needs_bot_section(self):
        StaffAccessGrant.objects.filter(user=self.staff).update(allowed_sections=["overview"])
        code = BotLinkCode.objects.create(user=self.staff)
        handlers.handle(msg(f"/link {code.code}"))
        self.assertFalse(BotLink.objects.exists())

    def test_deploy_and_destructive_scripts_superuser_only(self):
        self.link()
        StaffAccessGrant.objects.filter(user=self.staff).update(allowed_sections=["admin_bot", "scripts", "system_status"])
        handlers.handle(msg("/menu"))
        self.assertIn("d|scripts|", self.buttons())
        self.assertNotIn("d|deploy|", self.buttons())
        handlers.handle(press("d|deploy|"))
        self.assertIn("Нет доступа", self.last_text())
        with mock.patch.object(handlers, "has_2fa", return_value=True):
            handlers.handle(press("d|deploy_go|main"))
        self.assertIn("Нет прав", self.last_text())
        destructive = [s for _l, specs in handlers.bot_scripts().values() for s in specs if s.danger == "destructive"]
        if destructive:
            self.assertFalse(handlers._can_run(self.staff, destructive[0]))


class ScriptsAndDeployTests(BotTestCase):
    def setUp(self):
        super().setUp()
        self.staff.is_superuser = True
        self.staff.save()
        self.link()
        cache.set(f"adminbot:elev:dev:{self.staff.pk}", 1, 600)

    def test_scripts_list_and_run_notifies_on_finish(self):
        from dashboard.models import ManagementCommandRun

        handlers.handle(press("d|scripts|"))
        cats = [d for d in self.buttons() if "|scr_cat|" in d]
        self.assertTrue(cats)
        handlers.handle(press(cats[0]))
        spec = next(s for _l, specs in handlers.bot_scripts().values() for s in specs)
        handlers.handle(press(f"d|scr|{spec.name}"))
        self.assertIn(spec.name, self.last_text())
        mode = "dry" if spec.has_apply_flag else "run"
        with mock.patch.object(handlers, "has_2fa", return_value=True), mock.patch("dashboard.tasks.run_management_command.delay") as delay:
            delay.return_value.id = "task-1"
            handlers.handle(press(f"d|scr_run|{spec.name}:{mode}"))
        run = ManagementCommandRun.objects.get(command_name=spec.name)
        self.assertIn("запущена", self.last_text())
        self.assertEqual(handlers.check_runs(), 0)   # ещё выполняется
        run.status = ManagementCommandRun.Status.SUCCESS
        run.stdout = "\x1b[32mГотово\x1b[0m"
        run.save()
        self.assertEqual(handlers.check_runs(), 1)
        chat, text, _rows = self.sent[-1]
        self.assertEqual(chat, TID)
        self.assertIn("Готово", text)
        self.assertNotIn("\x1b", text)
        self.assertEqual(handlers.check_runs(), 0)   # второй раз не шлём

    @override_settings(ADMIN_BOT_GITHUB_TOKEN="gh", ADMIN_BOT_GITHUB_REPO="o/r")
    def test_deploy_and_rollback_dispatch(self):
        with mock.patch("adminbot.github.runs", return_value=[]), mock.patch("adminbot.github.releases", return_value=[{"tag": "v1.0.1", "published": ""}]):
            handlers.handle(press("d|deploy|"))
        self.assertIn("d|deploy_ask|v1.0.1", self.buttons())
        handlers.handle(press("d|deploy_ask|v1.0.1"))
        self.assertIn("откат", self.last_text())
        with mock.patch.object(handlers, "has_2fa", return_value=True), mock.patch("adminbot.github.dispatch") as dispatch:
            handlers.handle(press("d|deploy_go|v1.0.1"))
            dispatch.assert_called_once_with("v1.0.1")
            handlers.handle(press("d|deploy_go|main"))
            dispatch.assert_called_with("")
            handlers.handle(press("d|deploy_go|rm -rf"))
        self.assertIn("Неверная версия", self.last_text())


class ReviewQueuesTests(BotTestCase):
    def setUp(self):
        super().setUp()
        self.staff.is_superuser = True
        self.staff.save()
        self.link()
        cache.set(f"adminbot:elev:dev:{self.staff.pk}", 1, 600)

    def test_name_approve(self):
        from django.contrib.contenttypes.models import ContentType

        from parsers.models import NameVerificationSuggestion
        from players.models import Player

        p = Player.objects.create(first_name="Baurzhan", last_name="Islamkhan")
        s = NameVerificationSuggestion.objects.create(content_type=ContentType.objects.get_for_model(Player), object_id=str(p.pk),
                                                      entity_label="player", current_first_name="Baurzhan", current_last_name="Islamkhan",
                                                      suggested_first_name="Бауыржан", suggested_last_name="Исламхан", confidence="high")
        handlers.handle(press("d|names|"))
        self.assertIn("Бауыржан Исламхан", self.last_text())
        with mock.patch.object(handlers, "has_2fa", return_value=True):
            handlers.handle(press(f"d|name_ok|{s.pk}"))
        p.refresh_from_db()
        self.assertEqual(p.first_name, "Бауыржан")
        self.assertIn("Подтверждено", self.last_text())

    def test_duplicates_dismiss(self):
        from players.models import Player, PotentialDuplicatePlayer

        a = Player.objects.create(first_name="Иван", last_name="Петров")
        b = Player.objects.create(first_name="Иван", last_name="Петров")
        f = PotentialDuplicatePlayer.objects.create(existing_player=a, new_player=b)
        handlers.handle(press("d|dups|"))
        self.assertIn(f"d|dup_keep|{f.pk}:existing", self.buttons())
        with mock.patch.object(handlers, "has_2fa", return_value=True):
            handlers.handle(press(f"d|dup_diff|{f.pk}"))
        f.refresh_from_db()
        self.assertTrue(f.reviewed)


@override_settings(ADMIN_BOT_HEALTH_URL="")
class AlertsTests(BotTestCase):
    def test_alert_after_two_checks_reminder_mute_and_recovery(self):
        from . import alerts

        alerts.STATE.clear()
        alerts.MUTED.clear()
        sent = []
        send = lambda text, rows: sent.append(text)  # noqa: E731
        problem = alerts.Problem("celery", "Celery не работает", "Пульса не было", True)
        with mock.patch.object(alerts, "current_problems", return_value=[problem]):
            self.assertEqual(alerts.run_checks(send), [])          # первая проверка — ждём подтверждения
            self.assertEqual(alerts.run_checks(send), ["alert:celery"])
            self.assertEqual(alerts.run_checks(send), [])          # без спама
            self.assertEqual(len(alerts.active()), 1)
            alerts.STATE["celery"]["sent"] -= alerts.REMIND_EVERY + 1
            alerts.mute("celery")
            self.assertEqual(alerts.run_checks(send), [])          # заглушено
        with mock.patch.object(alerts, "current_problems", return_value=[]):
            self.assertEqual(alerts.run_checks(send), ["ok:celery"])
        self.assertIn("Celery не работает", sent[0])
        self.assertIn("Восстановлено", sent[-1])

    def test_checks_run_without_crashing(self):
        from . import alerts

        with mock.patch("dashboard.infra_services._redis_stats", return_value={"ok": True, "queue_depth": 0}):
            problems = alerts.current_problems()
        self.assertTrue(all(isinstance(p, alerts.Problem) for p in problems))


class CleanupTests(TestCase):
    def test_old_sample_drafts_dropped_fresh_kept(self):
        from .channel import drop_old_samples
        from .models import ChannelPost

        old = ChannelPost.objects.create(key="sample:1:x", kind="poll", text="a", status="draft")
        ChannelPost.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=2))
        fresh = ChannelPost.objects.create(key="sample:2:x", kind="poll", text="b", status="draft")
        real = ChannelPost.objects.create(key="poll:3", kind="poll", text="c", status="draft")
        ChannelPost.objects.filter(pk=real.pk).update(created_at=timezone.now() - timedelta(days=2))
        self.assertEqual(drop_old_samples(), 1)
        self.assertEqual(set(ChannelPost.objects.values_list("pk", flat=True)), {fresh.pk, real.pk})
