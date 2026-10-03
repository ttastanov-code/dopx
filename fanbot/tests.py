import hashlib
import hmac
import json
import time
from unittest import mock
from urllib.parse import urlencode

from django.test import TestCase, override_settings
from django.urls import reverse

from users.models import User

from . import bot, services
from .auth import verify_login_widget, verify_webapp
from .models import TelegramAccount, TelegramLinkCode

TOKEN = "123:fan-test-token"


def widget_payload(tg_id=777, auth_date=None, **extra):
    """Подписаны только поля Telegram; extra (next) — как в реальном адресе возврата, без подписи."""
    data = {"id": str(tg_id), "first_name": "Тимур", "username": "timur_kz", "auth_date": auth_date or str(int(time.time()))}
    check = "\n".join(f"{k}={data[k]}" for k in sorted(data))
    secret = hashlib.sha256(TOKEN.encode()).digest()
    data["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return {**data, **extra}


def webapp_init_data(tg_id=777, start="", allows=True, signature=False):
    pairs = {"auth_date": str(int(time.time())), "query_id": "q1",
             "user": json.dumps({"id": tg_id, "first_name": "Тимур", "username": "timur_kz", "allows_write_to_pm": allows})}
    if start:
        pairs["start_param"] = start
    if signature:
        pairs["signature"] = "ed25519-sig"  # новые клиенты: входит в строку для hash
    check = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    pairs["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(pairs)


@override_settings(FAN_BOT_TOKEN=TOKEN, FAN_BOT_USERNAME="dopx_fans_bot", FAN_BOT_APP_NAME="app")
class SignatureTests(TestCase):
    def test_widget_and_webapp_signatures(self):
        self.assertEqual(verify_login_widget(widget_payload(next="/x/"))["id"], 777)   # next не ломает подпись
        bad = widget_payload(); bad["username"] = "hacker"
        self.assertIsNone(verify_login_widget(bad))
        old = widget_payload(auth_date=str(int(time.time()) - 3 * 86400))
        self.assertIsNone(verify_login_widget(old))
        tg = verify_webapp(webapp_init_data(start="m_abc"))
        self.assertEqual((tg["id"], tg["start_param"], tg["allows_write"]), (777, "m_abc", True))
        self.assertIsNone(verify_webapp(webapp_init_data() + "x"))


@override_settings(FAN_BOT_TOKEN=TOKEN, FAN_BOT_USERNAME="dopx_fans_bot", FAN_BOT_APP_NAME="app")
class LoginFlowTests(TestCase):
    def test_widget_login_creates_verified_account_with_placeholder_email(self):
        resp = self.client.get(reverse("fanbot:login"), widget_payload(next="/matches/"))
        self.assertRedirects(resp, reverse("users:complete_profile") + "?next=%2Fmatches%2F", fetch_redirect_response=False)
        acc = TelegramAccount.objects.get(telegram_id=777)
        self.assertTrue(acc.user.is_verified)
        self.assertTrue(acc.user.email.endswith("@telegram.invalid"))
        self.assertEqual(acc.user.username, "timur_kz")
        self.assertFalse(acc.user.has_usable_password())
        self.client.logout()
        self.client.get(reverse("fanbot:login"), widget_payload())          # повторный вход — тот же аккаунт
        self.assertEqual(User.objects.count(), 1)

    def test_bad_signature_and_next_outside_site(self):
        bad = widget_payload(); bad["hash"] = "0" * 64
        self.assertRedirects(self.client.get(reverse("fanbot:login"), bad), reverse("users:login"), fetch_redirect_response=False)
        resp = self.client.get(reverse("fanbot:login"), widget_payload(next="https://evil.example/"))
        self.assertEqual(resp["Location"], reverse("users:complete_profile") + "?next=%2F")  # новый аккаунт — анкета

    def test_miniapp_auth_logs_in_and_routes_to_match(self):
        match_id = "11111111-2222-3333-4444-555555555555"
        resp = self.client.post(reverse("fanbot:miniapp_auth"), {"init_data": webapp_init_data(start=f"m_{match_id}")})
        self.assertEqual(resp.json()["next"], reverse("users:complete_profile") + f"?next=%2Fmatches%2F{match_id}%2F")
        self.assertTrue(TelegramAccount.objects.get(telegram_id=777).can_message)
        self.assertEqual(self.client.post(reverse("fanbot:miniapp_auth"), {"init_data": "x=1"}).status_code, 403)
        page = self.client.get(reverse("fanbot:miniapp"))
        self.assertIn("https://web.telegram.org", page["Content-Security-Policy"])
        self.assertNotIn("X-Frame-Options", page)

    def test_link_existing_account_by_code(self):
        user = User.objects.create_user(username="fan", email="fan@example.com", password="x", is_verified=True)
        self.client.force_login(user)
        resp = self.client.post(reverse("fanbot:link"))
        code = TelegramLinkCode.objects.get(user=user).code
        self.assertEqual(resp["Location"], f"https://t.me/dopx_fans_bot?start=link_{code}")
        with mock.patch("fanbot.services.call") as call:
            bot.handle({"message": {"chat": {"id": 900, "type": "private"}, "from": {"id": 900, "first_name": "Ф"},
                                    "text": f"/start link_{code}"}})
        acc = TelegramAccount.objects.get(user=user)
        self.assertEqual((acc.telegram_id, acc.can_message), (900, True))
        self.assertIn("привязан", call.call_args.kwargs["text"])
        with mock.patch("fanbot.services.call"):
            bot.handle({"message": {"chat": {"id": 900, "type": "private"}, "from": {"id": 900}, "text": "/stop"}})
        self.assertFalse(TelegramAccount.objects.get(user=user).notify)


@override_settings(FAN_BOT_TOKEN=TOKEN, FAN_BOT_USERNAME="dopx_fans_bot", FAN_BOT_APP_NAME="app",
                   VAPID_PRIVATE_KEY="", VAPID_PUBLIC_KEY="")
class NotifyTests(TestCase):
    def test_push_fan_out_also_goes_to_telegram_even_without_vapid(self):
        from notifications.services import send_push_to_users

        on = User.objects.create_user(username="a", email="a@example.com", password="x")
        off = User.objects.create_user(username="b", email="b@example.com", password="x")
        TelegramAccount.objects.create(user=on, telegram_id=1, can_message=True)
        TelegramAccount.objects.create(user=off, telegram_id=2, can_message=True, notify=False)
        with mock.patch("fanbot.services.call") as call, mock.patch("fanbot.services.SEND_PAUSE", 0):
            send_push_to_users([on.pk, off.pk], title="Матч закончился", body="Оцените игроков",
                               url="/matches/11111111-2222-3333-4444-555555555555/", kind="match_finished")
        self.assertEqual(call.call_count, 1)
        params = call.call_args.kwargs
        self.assertEqual(params["chat_id"], 1)
        self.assertEqual(params["reply_markup"]["inline_keyboard"][0][0]["url"],
                         "https://t.me/dopx_fans_bot/app?startapp=m_11111111-2222-3333-4444-555555555555")

    def test_blocked_bot_stops_messages_and_placeholder_gets_no_email(self):
        from adminbot.telegram import TelegramError
        from notifications.tasks import _send_email_to_user

        user = User.objects.create_user(username="c", email="tg5@telegram.invalid", password="x")
        TelegramAccount.objects.create(user=user, telegram_id=5, can_message=True)
        with mock.patch("fanbot.services.call", side_effect=TelegramError(403, "Forbidden: bot was blocked by the user")):
            self.assertFalse(services.send(5, "x"))
        self.assertFalse(TelegramAccount.objects.get(user=user).can_message)
        self.assertFalse(_send_email_to_user(user, "s", "emails/welcome.html", {}, force=True))


@override_settings(FAN_BOT_TOKEN=TOKEN, FAN_BOT_USERNAME="dopx_kz_bot", FAN_BOT_APP_NAME="", SITE_URL="https://dopx.kz")
class NoNewAppTests(TestCase):
    """Без /newapp: уведомления открывают Mini App web_app-кнопкой, /start m_<id> — кнопка на матч."""

    MATCH = "11111111-2222-3333-4444-555555555555"

    def test_notification_opens_miniapp_directly(self):
        user = User.objects.create_user(username="a", email="a@example.com", password="x")
        TelegramAccount.objects.create(user=user, telegram_id=1, can_message=True)
        with mock.patch("fanbot.services.call") as call, mock.patch("fanbot.services.SEND_PAUSE", 0):
            services.notify_users([user.pk], "Матч", "Оцените", f"/matches/{self.MATCH}/")
        btn = call.call_args.kwargs["reply_markup"]["inline_keyboard"][0][0]
        self.assertEqual(btn["web_app"]["url"], f"https://dopx.kz/tg/app/?start=m_{self.MATCH}")

    def test_start_match_payload_and_miniapp_start_from_query(self):
        with mock.patch("fanbot.services.call") as call:
            bot.handle({"message": {"chat": {"id": 9, "type": "private"}, "from": {"id": 9}, "text": f"/start m_{self.MATCH}"}})
        btn = call.call_args.kwargs["reply_markup"]["inline_keyboard"][0][0]
        self.assertTrue(btn["web_app"]["url"].endswith(f"?start=m_{self.MATCH}"))
        response = self.client.post(reverse("fanbot:miniapp_auth"), {"init_data": webapp_init_data(), "start": f"m_{self.MATCH}"})
        self.assertIn(reverse("matches:detail", args=[self.MATCH]).replace("/", "%2F"), response.json()["next"])

    def test_channel_posts_get_subscribe_button(self):
        from adminbot.channel import _button_rows
        from adminbot.models import ChannelPost

        rows = _button_rows(ChannelPost(text="x", buttons=[["DOPX", "https://dopx.kz"]]))
        self.assertEqual(rows[-1], [("🔔 Уведомления о матчах", "https://t.me/dopx_kz_bot?start=channel")])
        with override_settings(FAN_BOT_TOKEN=""):
            self.assertEqual(len(_button_rows(ChannelPost(text="x", buttons=[]))), 0)


    def test_leaves_groups(self):
        with mock.patch("fanbot.services.call") as call:
            bot.handle({"my_chat_member": {"chat": {"id": -100, "type": "supergroup"},
                                           "new_chat_member": {"status": "member"}}})
        call.assert_called_once_with("leaveChat", chat_id=-100)


@override_settings(FAN_BOT_TOKEN=TOKEN, FAN_BOT_USERNAME="dopx_kz_bot")
class WebEnterTests(TestCase):
    def test_enter_link_logs_in_once(self):
        data = self.client.post(reverse("fanbot:miniapp_auth"), {"init_data": webapp_init_data()}).json()
        self.client.logout()
        first = self.client.get(data["enter_url"])
        self.assertEqual(first.url, data["next"])
        self.assertIn("_auth_user_id", self.client.session)
        self.client.logout()
        self.client.get(data["enter_url"])  # повтор — без входа
        self.assertNotIn("_auth_user_id", self.client.session)
        bad = self.client.get(reverse("fanbot:miniapp_enter") + "?t=junk")
        self.assertEqual(bad.url, reverse("users:login"))


@override_settings(FAN_BOT_TOKEN=TOKEN)
class SignatureFieldTests(TestCase):
    def test_init_data_with_signature_field(self):
        self.assertIsNotNone(verify_webapp(webapp_init_data(signature=True)))
        self.assertIsNotNone(verify_webapp(webapp_init_data()))
        self.assertIsNone(verify_webapp(webapp_init_data(signature=True) + "&extra=1"))


@override_settings(FAN_BOT_TOKEN=TOKEN, FAN_BOT_USERNAME="dopx_kz_bot")
class LinkOverEmptyTelegramAccountTests(TestCase):
    def test_empty_telegram_account_gives_way_to_main(self):
        tg = {"id": 555, "username": "timur_kz", "first_name": "Тимур"}
        empty = services.account_for(tg).user
        main = User.objects.create_user(username="main", email="main@example.com", password="x")
        code = TelegramLinkCode.objects.create(user=main)
        ok, _ = services.link_by_code(code.code, tg)
        self.assertTrue(ok)
        self.assertEqual(TelegramAccount.objects.get(telegram_id=555).user, main)
        self.assertFalse(User.objects.filter(pk=empty.pk).exists())

    def test_account_with_activity_is_kept(self):
        from predictions.models import MatchPrediction
        from matches.models import Match

        tg = {"id": 556, "username": "x", "first_name": "X"}
        busy = services.account_for(tg).user
        with mock.patch.object(MatchPrediction.objects, "filter") as f:
            f.return_value.exists.return_value = True
            main = User.objects.create_user(username="main2", email="m2@example.com", password="x")
            ok, msg = services.link_by_code(TelegramLinkCode.objects.create(user=main).code, tg)
        self.assertFalse(ok)
        self.assertIn("оценки или прогнозы", msg)
        self.assertTrue(User.objects.filter(pk=busy.pk).exists())

    def test_placeholder_email_hidden(self):
        user = services.account_for({"id": 557, "username": "y", "first_name": "Y"}).user
        self.assertFalse(user.has_real_email)
        self.client.force_login(user)
        page = self.client.get(reverse("users:profile"))
        self.assertNotContains(page, "telegram.invalid")
        self.assertContains(page, "добавить почту")


@override_settings(FAN_BOT_TOKEN=TOKEN, FAN_BOT_USERNAME="dopx_kz_bot", STAFF_2FA_ENFORCED=False)
class WelcomeFlowTests(TestCase):
    def _login_new(self):
        response = self.client.get(reverse("fanbot:login") + "?" + urlencode(widget_payload(next="/matches/")))
        self.assertTrue(response.url.startswith(reverse("users:complete_profile")))
        return User.objects.get(telegram__telegram_id=777)

    def test_new_account_fills_profile_and_confirms_email(self):
        from users import emails

        user = self._login_new()
        page = self.client.get(reverse("users:complete_profile") + "?next=/matches/")
        self.assertContains(page, "Уже есть аккаунт DOPX?")
        self.assertNotContains(page, "telegram.invalid")
        with mock.patch("notifications.tasks.send_email_change_confirmation.delay") as mail, self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("users:complete_profile"), {"action": "profile", "next": "/matches/",
                                                                "city": "Алматы", "email": "timur@example.com"})
        mail.assert_called_once()
        user.refresh_from_db()
        # Почта ещё не сменилась — ждёт подтверждения; Telegram-аккаунт остаётся активным.
        self.assertEqual((user.city, user.pending_email, user.is_verified), ("Алматы", "timur@example.com", True))
        self.assertFalse(user.has_real_email)
        confirmed, error = emails.confirm_change(user.verification_token)
        self.assertEqual(error, "")
        user.refresh_from_db()
        self.assertTrue(user.profile_complete)
        self.client.logout()
        again = self.client.get(reverse("fanbot:login") + "?" + urlencode(widget_payload(next="/matches/")))
        self.assertEqual(again.url, "/matches/")

    def test_email_of_existing_account_suggests_merge_and_merge_works(self):
        main = User.objects.create_user(username="main", email="main@example.com", password="Secret-pass-1")
        empty = self._login_new()
        page = self.client.post(reverse("users:complete_profile"), {"action": "profile", "city": "Алматы", "email": "main@example.com"})
        self.assertContains(page, "войдите в него ниже")
        response = self.client.post(reverse("users:complete_profile"), {"action": "merge", "next": "/matches/",
                                                                        "username": "main@example.com", "password": "Secret-pass-1"})
        self.assertEqual(response.url, "/matches/")
        self.assertEqual(TelegramAccount.objects.get(telegram_id=777).user, main)
        self.assertFalse(User.objects.filter(pk=empty.pk).exists())
        self.assertEqual(self.client.session["_auth_user_id"], str(main.pk))

    def test_wrong_password_and_skip(self):
        User.objects.create_user(username="main", email="main@example.com", password="Secret-pass-1")
        empty = self._login_new()
        page = self.client.post(reverse("users:complete_profile"), {"action": "merge", "username": "main", "password": "bad"})
        self.assertEqual(page.status_code, 200)
        self.assertTrue(User.objects.filter(pk=empty.pk).exists())
        response = self.client.post(reverse("users:complete_profile"), {"action": "skip", "next": "/matches/"})
        self.assertEqual(response.url, "/matches/")


@override_settings(FAN_BOT_TOKEN=TOKEN, FAN_BOT_USERNAME="dopx_kz_bot")
class ProfileTelegramBlockTests(TestCase):
    def test_status_relink_and_unlink_guard(self):
        user = services.account_for({"id": 901, "username": "fan_tg", "first_name": "F"}).user
        self.client.force_login(user)
        page = self.client.get(reverse("users:profile"))
        self.assertContains(page, "@fan_tg")
        self.assertContains(page, "Перепривязать")
        # Без пароля отвязать нельзя — Telegram единственный вход.
        self.client.post(reverse("fanbot:unlink"), {"next": "/users/profile/"})
        self.assertTrue(TelegramAccount.objects.filter(user=user).exists())
        user.set_password("Secret-pass-1")
        user.save()
        self.client.force_login(user)
        self.client.post(reverse("fanbot:unlink"), {"next": "/users/profile/"})
        self.assertFalse(TelegramAccount.objects.filter(user=user).exists())
        # Перепривязка: ссылка из другого Telegram обновляет привязку.
        code = TelegramLinkCode.objects.create(user=user)
        ok, _ = services.link_by_code(code.code, {"id": 902, "username": "new_tg", "first_name": "N"})
        self.assertTrue(ok)
        self.assertEqual(TelegramAccount.objects.get(user=user).telegram_id, 902)
