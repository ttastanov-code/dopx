# core/tests_heartbeat.py
"""Пульс сервисов: статусы, перезапуск по кнопке, страница «Системный статус», сторож бота команды."""
import time
from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from core import heartbeat
from users.models import User


@override_settings(FAN_BOT_TOKEN="1:x", STAFF_2FA_ENFORCED=False)
class HeartbeatTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_statuses(self):
        heartbeat.beat("fan_bot", updates=5, errors=1, last_error="boom")
        cache.set("hb:celery_realtime", {"ts": time.time() - 400, "version": "dev", "started": time.time() - 500})
        rows = {r["name"]: r for r in heartbeat.overview()}
        self.assertEqual(rows["fan_bot"]["status"], "ok")
        self.assertEqual(rows["celery_realtime"]["status"], "stale")
        self.assertEqual(rows["celery_worker"]["status"], "down")
        with override_settings(FAN_BOT_TOKEN=""):
            self.assertEqual({r["name"]: r for r in heartbeat.overview()}["fan_bot"]["status"], "off")

    def test_task_done_marks_worker_alive_and_throttles(self):
        from core.health import celery_heartbeat_age

        heartbeat._last_task_beat = 0.0
        heartbeat.task_done("celery@box")
        heartbeat.task_done("realtime@box")  # в пределах 30 с — пропуск
        rows = {r["name"]: r for r in heartbeat.overview()}
        self.assertEqual(rows["celery_worker"]["status"], "ok")
        self.assertEqual(rows["celery_realtime"]["status"], "down")
        self.assertEqual(celery_heartbeat_age(), 0)
        heartbeat._last_task_beat = 0.0
        heartbeat.task_done("realtime@box")
        self.assertEqual({r["name"]: r for r in heartbeat.overview()}["celery_realtime"]["status"], "ok")

    def test_restart_flag_only_for_older_process(self):
        self.assertFalse(heartbeat.restart_requested("fan_bot"))
        heartbeat.request_restart("fan_bot")
        self.assertTrue(heartbeat.restart_requested("fan_bot"))
        with mock.patch.object(heartbeat, "STARTED", time.time() + 10):  # процесс уже перезапустился
            self.assertFalse(heartbeat.restart_requested("fan_bot"))

    def test_page_and_restart_button(self):
        heartbeat.beat("fan_bot", updates=3, errors=0)
        admin = User.objects.create_superuser(username="boss", email="b@t.local", password="x")
        self.client.force_login(admin)
        page = self.client.get(reverse("dashboard:system_status"))
        self.assertContains(page, "Бот болельщиков")
        self.assertContains(page, "апдейтов 3")
        self.client.post(reverse("dashboard:service_restart", args=["fan_bot"]))
        self.assertTrue(heartbeat.restart_requested("fan_bot"))
        # Сайт так не перезапускается.
        self.client.post(reverse("dashboard:service_restart", args=["web"]))
        self.assertFalse(heartbeat.restart_requested("web"))

    @override_settings(ADMIN_BOT_TOKEN="2:y")
    def test_watchdog_alerts_once(self):
        from adminbot.tasks import watch_admin_bot

        with mock.patch("adminbot.alerts.enabled", return_value=True), \
                mock.patch("adminbot.telegram.enabled", return_value=True), \
                mock.patch("adminbot.notify.recipients", return_value=[42]), \
                mock.patch("adminbot.telegram.send") as send:
            watch_admin_bot()
            watch_admin_bot()  # второй раз — без спама
        self.assertEqual(send.call_count, 1)
        self.assertIn("Бот команды не отвечает", send.call_args.args[1])
